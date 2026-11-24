# UPnP-Audirvana

`v0.1.0-preview.2` · Renderer UPnP AV para Audirvāna con MPD 0.24.5 y el frontend de upmpdcli 1.9.17 enlazados en un ejecutable. El control de MPD circula por un socket Unix privado. El servicio no inicia un segundo daemon ni publica el puerto TCP de MPD.

El objetivo de esta primera versión es Raspberry Pi 3 y 4 con DietPi. El paquete ARM64 se compila sobre Debian 13. Para una instalación de 32 bits (`armhf`), el mismo script compila un paquete nativo sobre DietPi Debian 13. La validación con Audirvāna y un DAC físico queda para la fase siguiente.

## Qué incluye

- UPnP AV con descubrimiento, eventos de estado, transporte, posición, búsqueda y preparación de la siguiente pista.
- Soporte de consultas de volumen y rango en dB, estado fijo de 100 % si MPD no tiene mezclador y control con el mezclador ALSA si se configura.
- Reinicio de la pista actual y borrado de la siguiente mediante URI vacía. El segundo conserva la pista activa.
- HTTP/HTTPS de entrada, FLAC, WAV, AIFF, MP3, PCM L16/L24 y decodificadores DSD para DSF/DFF. La configuración usa ALSA directo y desactiva DoP. La salida DSD nativa depende del DAC, su controlador y el formato de entrada.
- OpenHome y MediaServer desactivados. MPD compilado sin base de datos musical.

`preview-0.2` de [gmrender-audirvana](https://github.com/tinker-net/gmrender-audirvana/tree/preview-0.2) es la referencia de comportamiento para las pruebas. La reproducción hasta el final de la cola, gapless, la negociación DSD64 y el ahorro real de memoria siguen pendientes de medir en la Pi. El error WebSocket 401 de Audirvāna también se comprobará durante esas pruebas.

## Cambios de la segunda preview

- Con volumen fijo, el renderer acepta las órdenes válidas de volumen y mute y conserva el nivel al 100 %. Con mezclador ALSA configurado, delega los cambios en MPD.
- Las respuestas SOAP conservan la URI HTTP original, incluidas las consultas con `&` y escapes como `%2F`. Una referencia sin servidor, como `test.pcm`, se rechaza antes de modificar la cola.
- Play, Pause, Stop y la limpieza de la siguiente pista toleran repeticiones. Los eventos distinguen la pista siguiente por el identificador de cola aunque reutilice la misma URL.
- GitHub Actions comprueba SSDP, las descripciones de servicio, las suscripciones GENA y las llamadas SOAP en el binario empaquetado.

Para actualizar desde la primera preview, instala el contenido de `opt/upnp-audirvana` del nuevo archivo con el servicio parado. Conserva los archivos de `/etc/upnp-audirvana` y vuelve a arrancar el servicio.

## Compilar en DietPi Debian 13

En DietPi como root, omite `sudo`:

```sh
sudo apt-get update
sudo apt-get install -y build-essential ca-certificates curl git meson ninja-build pkg-config \
  libasound2-dev libcurl4-openssl-dev libexpat1-dev libflac-dev libfmt-dev \
  libid3tag0-dev libjsoncpp-dev libmicrohttpd-dev libmpdclient-dev libmpg123-dev libsndfile1-dev
git clone https://github.com/tinker-net/UPnP-Audirvana.git
cd UPnP-Audirvana
./build.sh
```

El script genera un archivo `UPnP-Audirvana-v0.1.0-preview.2-<arquitectura>.tar.gz`. Usa dos tareas de compilación por defecto. Puedes reducir a una con `JOBS=1 ./build.sh` en una Pi con poca memoria.

## Instalar junto al renderer actual

La primera instalación copia el servicio y la configuración a rutas propias. Extrae el archivo de la arquitectura de tu DietPi:

```sh
sudo apt-get install -y libasound2t64 libcurl4t64 libexpat1 libflac14 libfmt10 \
  libid3tag0 libjsoncpp26 libmicrohttpd12t64 libmpdclient2t64 libmpg123-0t64 libsndfile1
sudo tar --extract --gzip --keep-old-files --file UPnP-Audirvana-v0.1.0-preview.2-arm64.tar.gz -C /
sudo systemctl daemon-reload
```

Comprueba el identificador de tu DAC con `aplay -l`. Ajusta `device` en `/etc/upnp-audirvana/mpd.conf` antes de arrancar. Por ejemplo, `hw:CARD=D10s,DEV=0` o `hw:CARD=Lite,DEV=0`. Para volumen fijo conserva `mixer_type "none"`. Para un mezclador de hardware, usa `mixer_type "hardware"` y configura el nombre exacto del control con `mixer_control`. El D10s puede anunciar el control como `"D10s "`, con espacio final.

Detén temporalmente el servicio que esté usando el mismo DAC y arranca el nuevo:

```sh
sudo systemctl start upnp-audirvana.service
journalctl -u upnp-audirvana.service -n 60 --no-pager
```

En Audirvāna, selecciona **UPnP-Audirvana** como salida. Si el host tiene varias interfaces, puedes fijar `upnpiface = eth0` o `wlan0` en `/etc/upnp-audirvana/renderer.conf` y reiniciar el servicio. Para volver a la instalación anterior, detén `upnp-audirvana.service` y arranca el servicio previo. El arranque automático del renderer nuevo se activa explícitamente con `sudo systemctl enable upnp-audirvana.service`.

## Código y licencias

El repositorio contiene los parches y el código de empaquetado. El script recupera las revisiones exactas de [MPD](https://github.com/MusicPlayerDaemon/MPD) y del [proyecto upmpdcli](https://www.lesbonscomptes.com/upmpdcli/pages/downloads.html) y verifica los hashes de las fuentes. El ejecutable combinado se distribuye bajo GPL-2.0-or-later. El paquete incluye los avisos de licencia de los componentes.
