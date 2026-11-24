#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
set -eu

project=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
build="$project/.build"
prefix=/opt/upnp-audirvana
mpd_revision=35df68a2dd11fe7e873c54d966f227328b52f4fa
jobs=${JOBS:-2}

mkdir -p "$build/cache"
work=$(mktemp -d "$build/work.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 1' INT TERM HUP

fetch() {
    name=$1
    expected=$2
    tarball="$build/cache/$name.tar.gz"
    if ! printf '%s  %s\n' "$expected" "$tarball" | sha256sum -c --status; then
        curl --fail --location --silent --show-error --retry 2 \
            "https://www.lesbonscomptes.com/upmpdcli/downloads/$name.tar.gz" \
            -o "$work/$name.tar.gz"
        printf '%s  %s\n' "$expected" "$work/$name.tar.gz" | sha256sum -c
        mv "$work/$name.tar.gz" "$tarball"
    fi
    tar -xzf "$tarball" -C "$work"
}

fetch libnpupnp-6.3.1 1e6271dd2e641a5b095d994be6976031e5f6b32a22df3bb1988636f51b0f52b7
fetch libupnpp-1.0.4 4738a19be51c09bd59a26b28e305172e2052c0e970b2fad92320a2d7cf1157c5
fetch upmpdcli-1.9.17 5eb9ba2f4d2790e302fc1788509789804bb866161e4d9ae05e13bc6f75fc4e1f
(cd "$work/libnpupnp-6.3.1" && git apply "$project/patches/libnpupnp.patch")

if [ ! -d "$build/cache/mpd.git" ]; then
    git clone --bare --depth 1 --branch v0.24.5 \
        https://github.com/MusicPlayerDaemon/MPD.git "$work/mpd.git"
    test "$(git -C "$work/mpd.git" rev-parse HEAD)" = "$mpd_revision"
    mv "$work/mpd.git" "$build/cache/mpd.git"
fi
git clone --quiet --no-checkout "$build/cache/mpd.git" "$work/mpd"
git -C "$work/mpd" checkout --quiet --detach "$mpd_revision"
git -C "$work/mpd" apply "$project/patches/mpd.patch"
renderer="$work/mpd/subprojects/audirvana-renderer"
mv "$work/upmpdcli-1.9.17" "$renderer"
git -C "$work/mpd" apply --directory=subprojects/audirvana-renderer "$project/patches/upmpdcli.patch"

# Build the two UPnP libraries against a temporary prefix. Link the executable
# against them there and package their runtime copies under the final prefix.
deps="$work/deps"
export PKG_CONFIG_PATH="$deps/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LDFLAGS="${LDFLAGS:-} -Wl,-rpath-link,$deps/lib"

for lib in libnpupnp-6.3.1 libupnpp-1.0.4; do
    meson setup "$work/build-$lib" "$work/$lib" \
        --prefix="$deps" --libdir=lib --buildtype=release
    meson compile -C "$work/build-$lib" -j "$jobs"
    meson install -C "$work/build-$lib"
done

meson setup "$work/build-mpd" "$work/mpd" --prefix="$prefix" \
    --buildtype=release --wrap-mode=nodownload --auto-features=disabled \
    -Dalsa=enabled -Dcurl=enabled -Dflac=enabled -Dsndfile=enabled \
    -Dmpg123=enabled -Did3tag=enabled -Ddsd=true \
    -Ddatabase=false -Dneighbor=false -Dcue=false -Dupnp=disabled \
    -Dtcp=false -Dlocal_socket=true -Ddaemon=false -Dinotify=false \
    -Dhttpd=false -Dsnapcast=false -Drecorder=false -Dpipe=false \
    -Dwave_encoder=false -Ddocumentation=disabled
meson compile -C "$work/build-mpd" -j "$jobs"
meson install -C "$work/build-mpd" --destdir "$work/stage"

stage="$work/stage$prefix"
mkdir -p "$stage/lib" "$stage/share/licenses"
cp -P "$deps"/lib/libnpupnp.so* "$deps"/lib/libupnpp.so* "$stage/lib/"
cp "$work/mpd/COPYING" "$stage/share/licenses/MPD"
cp "$renderer/COPYING" "$stage/share/licenses/upmpdcli"
cp "$work/libupnpp-1.0.4/COPYING" "$stage/share/licenses/libupnpp"
cp "$work/libnpupnp-6.3.1/COPYING" "$stage/share/licenses/libnpupnp"

mkdir -p "$work/stage/etc/upnp-audirvana" "$work/stage/etc/systemd/system"
cp "$project"/packaging/*.conf "$work/stage/etc/upnp-audirvana/"
cp "$project/packaging/upnp-audirvana.service" "$work/stage/etc/systemd/system/"
archive="UPnP-Audirvana-v0.1.0-preview.4-$(dpkg --print-architecture).tar.gz"
tar -czf "$work/$archive" -C "$work/stage" opt etc
mv "$work/$archive" "$project/$archive"
printf '\n%s\n' "$project/$archive"

if [ "${SOURCE_BUNDLE:-0}" = 1 ]; then
    source_dir="$work/UPnP-Audirvana-source"
    mkdir -p "$source_dir/upstream"
    cp "$project"/build.sh "$project"/README.md "$project"/COPYING "$source_dir/"
    cp -R "$project"/patches "$project"/packaging "$project"/tests "$source_dir/"
    git -C "$work/mpd" archive --format=tar.gz --prefix=mpd-0.24.5/ \
        "$mpd_revision" > "$source_dir/upstream/mpd-0.24.5.tar.gz"
    cp "$build/cache/"*.tar.gz "$source_dir/upstream/"
    tar -czf "$work/UPnP-Audirvana-v0.1.0-preview.4-source.tar.gz" \
        -C "$work" UPnP-Audirvana-source
    mv "$work/UPnP-Audirvana-v0.1.0-preview.4-source.tar.gz" "$project/"
fi
