"""Exercise the packaged renderer over SSDP, HTTP/GENA and SOAP."""
import http.client
import io
import os
from pathlib import Path
from queue import Queue
import socket
import subprocess
import sys
import time
import tempfile
from threading import Thread
from urllib.parse import urljoin, urlsplit
import wave
from xml.etree import ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEVICE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
WAVE = io.BytesIO()
with wave.open(WAVE, "wb") as wav:
    wav.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
    wav.writeframes(bytes(44100 * 4 * 5))


def request(method, url, headers=None, body=None):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(method, path, body=body, headers=headers or {})
        result = conn.getresponse()
        return result.status, dict(result.getheaders()), result.read()
    finally:
        conn.close()


def discover(ip):
    multicast = ("239.255.255.250", 1900)
    message = (
        "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\nMX: 1\r\n'
        f"ST: {DEVICE}\r\n\r\n"
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((ip, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        sock.settimeout(1)
        for _ in range(25):
            sock.sendto(message.encode(), multicast)
            try:
                packet, _ = sock.recvfrom(65535)
            except TimeoutError:
                continue
            lines = packet.decode().splitlines()
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            location = next((v.strip() for k, v in headers.items() if k.lower() == "location"), "")
            if location:
                return location
    raise AssertionError("SSDP did not return the renderer description")


def main(prefix):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("239.255.255.250", 1900))
        ip = route.getsockname()[0]

    events = Queue()
    media_requests = Queue()

    class Server(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_NOTIFY(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            events.put((self.path, payload))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            media_requests.put(self.path)
            data = WAVE.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer((ip, 0), Server) as server:
        root = Path(tmp)
        host = f"http://{ip}:{server.server_port}"
        Thread(target=server.serve_forever, daemon=True).start()
        (root / "mpd.conf").write_text(f"""bind_to_address "{root}/mpd.sock"
      audio_output {{
          type "null"
          name "Test output"
          mixer_type "none"
      }}
""")
        (root / "renderer.conf").write_text(f"""friendlyname = UPnP-Audirvana-WireTest
mpdhost = {root}/mpd.sock
pkgdatadir = {prefix}/share/upnp-audirvana
cachedir = {root}/cache
upnpip = {ip}
ownqueue = 1
ohmetapersist = 0
logfilename = stderr
""")
        # The systemd service uses an unprivileged user. Run the same way in CI.
        log = root / "renderer.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                [prefix / "bin/upnp-audirvana", "--no-daemon", "--stderr", root / "mpd.conf"],
                env={**os.environ, "UPNP_AUDIRVANA_CONFIG": str(root / "renderer.conf")},
                stdout=output,
                stderr=output,
            )
            try:
                location = discover(ip)
                check(location, host, events, media_requests)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    code = process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                finally:
                    print(log.read_text(), file=sys.stderr)
                    server.shutdown()
            assert code == 0, code
            assert "ContentDirectory::makerootdir" not in log.read_text()


def check(location, host, events, media_requests):
    status, _, payload = request("GET", location)
    assert status == 200, status
    desc = ET.fromstring(payload)
    services = {}

    for svc in desc.findall(".//{*}service"):
        name = svc.findtext("{*}serviceType")
        kind = name.split(":")[-2]
        control, event, scpd = (svc.findtext("{*}" + key) for key in
                                ("controlURL", "eventSubURL", "SCPDURL"))
        assert all(path.startswith("/") for path in (control, event, scpd)), (control, event, scpd)
        services[kind] = name, urljoin(location, control), urljoin(location, event)
        status, _, document = request("GET", urljoin(location, scpd))
        assert status == 200, status
        actions = ET.fromstring(document).find("{*}actionList")
        assert actions is not None
        if kind == "AVTransport":
            action = next(a for a in actions if a.findtext("{*}name") == "GetCurrentTransportActions")
            assert action.find("{*}Optional") is None
            args = action.findall("./{*}argumentList/{*}argument")
            assert [(a.findtext("{*}name"), a.findtext("{*}direction")) for a in args] == [
                ("InstanceID", "in"), ("Actions", "out"),
            ]

    assert services.keys() == {"AVTransport", "RenderingControl", "ConnectionManager"}

    def soap(kind, action, fault=None, **args):
        namespace, control, _ = services[kind]
        body = ET.SubElement(ET.Element(f"{{{SOAP}}}Envelope"), f"{{{SOAP}}}Body")
        command = ET.SubElement(body, f"{{{namespace}}}{action}")
        for key, value in args.items():
            ET.SubElement(command, key).text = str(value)
        envelope = ET.Element(f"{{{SOAP}}}Envelope")
        envelope.append(body)
        status, headers, response = request("POST", control, {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{namespace}#{action}"',
        }, ET.tostring(envelope, short_empty_elements=False))
        document = ET.fromstring(response)
        if fault:
            assert document.findtext(".//{*}errorCode") == str(fault), (action, response)
            return
        assert status == 200, (action, status, response)
        assert any(key.lower() == "content-type" and value.startswith("text/xml")
                   for key, value in headers.items()), (action, headers)
        result = document.find(f".//{{{namespace}}}{action}Response")
        assert result is not None, (action, response)
        return {element.tag: element.text or "" for element in result}

    def avt(action, **args):
        return soap("AVTransport", action, InstanceID=0, **args)

    def rcs(action, **args):
        return soap("RenderingControl", action, InstanceID=0, Channel="Master", **args)

    for kind in ("AVTransport", "RenderingControl"):
        _, _, event_url = services[kind]
        path = f"/events/{kind}"
        status, headers, _ = request("SUBSCRIBE", event_url, {
            "CALLBACK": f"<{host}{path}>", "NT": "upnp:event", "TIMEOUT": "Second-1800",
        })
        assert status == 200, (kind, status, headers)
        callback, payload = events.get(timeout=5)
        assert callback == path, (kind, callback)
        last_change = ET.fromstring(payload).findtext(".//{*}LastChange")
        assert ET.fromstring(last_change).tag.endswith("/" + ("AVT" if kind == "AVTransport" else "RCS") + "}Event"), last_change

    assert "audio/wav" in soap("ConnectionManager", "GetProtocolInfo")["Sink"]
    avt("SetPlayMode", NewPlayMode="NORMAL")
    assert avt("GetCurrentTransportActions")["Actions"] == ""
    soap("AVTransport", "SetPlayMode", InstanceID=0, fault=712, NewPlayMode="SHUFFLE")
    soap("AVTransport", "GetTransportInfo", InstanceID=99, fault=718)
    assert avt("GetTransportSettings")["PlayMode"] == "NORMAL"
    assert rcs("GetVolume")["CurrentVolume"] == "100"
    assert rcs("GetVolumeDB")["CurrentVolume"] == "0"
    assert rcs("GetVolumeDBRange")["MaxValue"] == "0"
    rcs("SetVolume", DesiredVolume=35)
    rcs("SetVolumeDB", DesiredVolume=-2048)
    rcs("SetMute", DesiredMute=1)
    assert rcs("GetVolume")["CurrentVolume"] == "100"
    assert rcs("GetMute")["CurrentMute"] == "0"

    uri = f"{host}/test.pcm?token=a%2Fb&part=1"
    metadata = '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="1"/></DIDL-Lite>'
    avt("SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=metadata)
    assert avt("GetMediaInfo")["CurrentURI"] == uri
    assert avt("GetMediaInfo")["CurrentURIMetaData"] == metadata
    assert avt("GetCurrentTransportActions")["Actions"] == "Play"
    avt("SetNextAVTransportURI", NextURI=uri, NextURIMetaData=metadata)
    assert avt("GetMediaInfo")["NextURI"] == uri
    avt("SetNextAVTransportURI", NextURI="", NextURIMetaData="")
    assert avt("GetMediaInfo")["NextURI"] == ""
    soap("AVTransport", "SetAVTransportURI", InstanceID=0, fault=716,
         CurrentURI="test.pcm", CurrentURIMetaData="")
    assert avt("GetMediaInfo")["CurrentURI"] == uri
    avt("Play", Speed=1)
    assert media_requests.get(timeout=5) == "/test.pcm?token=a%2Fb&part=1"
    assert avt("GetPositionInfo")["TrackURI"] == uri
    assert avt("GetCurrentTransportActions")["Actions"] == "Pause,Stop,Seek"
    assert avt("GetTransportInfo")["CurrentTransportState"] == "PLAYING"
    avt("Pause")
    avt("Pause")
    assert avt("GetTransportInfo")["CurrentTransportState"] == "PAUSED_PLAYBACK"
    assert avt("GetCurrentTransportActions")["Actions"] == "Play,Stop,Seek"
    avt("Play", Speed=1)
    avt("Play", Speed=1)
    avt("SetNextAVTransportURI", NextURI="", NextURIMetaData="")
    assert avt("GetTransportInfo")["CurrentTransportState"] == "PLAYING"
    avt("Stop")
    avt("Stop")
    avt("SetAVTransportURI", CurrentURI="", CurrentURIMetaData="")
    assert avt("GetMediaInfo")["CurrentURI"] == ""
    assert avt("GetTransportInfo")["CurrentTransportState"] == "STOPPED"
    assert avt("GetCurrentTransportActions")["Actions"] == ""
    next_metadata = metadata.replace('id="1"', 'id="2"')
    avt("SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=metadata)
    avt("SetNextAVTransportURI", NextURI=uri, NextURIMetaData=next_metadata)
    avt("Play", Speed=1)
    deadline = time.monotonic() + 14
    promoted = False
    started = time.monotonic()
    while time.monotonic() < deadline:
        media = avt("GetMediaInfo")
        if media["CurrentURIMetaData"] == next_metadata:
            promoted = True
            assert media["NextURI"] == ""
        if promoted and avt("GetTransportInfo")["CurrentTransportState"] == "STOPPED":
            break
        time.sleep(0.2)
    else:
        raise AssertionError("Queued duplicate URI did not advance and stop")
    assert time.monotonic() - started >= 9, "The final five-second track stopped early"
    print("PASS SSDP, SCPD, GENA, SOAP, fixed volume, HTTP URI round trip and two-track EOS")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
