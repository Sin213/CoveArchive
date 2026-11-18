#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
export TEMP="$PWD/build/release/tmp" TMP="$PWD/build/release/tmp" TMPDIR="$PWD/build/release/tmp"
export XDG_CONFIG_HOME="$PWD/build/release/config" XDG_STATE_HOME="$PWD/build/release/state"
mkdir -p "$TMPDIR" dist/release
python -m pytest
python -m PyInstaller --noconfirm --workpath build/release/work-linux --distpath build/release/dist-linux packaging/covearchive.spec
binary="$PWD/build/release/dist-linux/CoveArchive/CoveArchive"
python packaging/verify_package.py "$(dirname "$binary")" --platform linux
for diagnostic in --smoke-test --package-selftest; do
    xvfb-run -a "$binary" "$diagnostic"
    QT_QPA_PLATFORM=offscreen "$binary" "$diagnostic"
done
bash packaging/linux/build-appimage.sh "$(dirname "$binary")" dist/release
bash packaging/linux/build-deb.sh "$(dirname "$binary")" dist/release
image="$PWD/dist/release/CoveArchive-1.0.1-x86_64.AppImage"
for diagnostic in --smoke-test --package-selftest; do
    xvfb-run -a env APPIMAGE_EXTRACT_AND_RUN=1 "$image" --appimage-extract-and-run "$diagnostic"
done
mkdir build/release/image-extract
(cd build/release/image-extract && "$image" --appimage-extract)
python packaging/verify_package.py build/release/image-extract/squashfs-root --platform linux
# Verification image; digest of the ubuntu:22.04 image index from the Docker Hub registry.
docker run --rm -v "$PWD/dist/release:/artifacts:ro" ubuntu:22.04@sha256:9abcaffc92a5279fff3a6a41635c0800ea33e528425fb389658806476e7defde bash -euc '
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y xvfb /artifacts/covearchive_1.0.1_amd64.deb
  export XDG_CONFIG_HOME=/tmp/config XDG_STATE_HOME=/tmp/state
  xvfb-run -a covearchive --smoke-test
  xvfb-run -a covearchive --package-selftest
  QT_QPA_PLATFORM=offscreen covearchive --smoke-test
  QT_QPA_PLATFORM=offscreen covearchive --package-selftest
'
(cd dist/release && sha256sum CoveArchive-1.0.1-x86_64.AppImage covearchive_1.0.1_amd64.deb > linux.sha256)
{ printf 'sha256<<CHECKSUMS\n'; cat dist/release/linux.sha256; printf 'CHECKSUMS\n'; } >> "$GITHUB_OUTPUT"
