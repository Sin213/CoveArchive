#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
init "$@"
: "${APPIMAGETOOL_VERSION:?workflow env required}" "${APPIMAGETOOL_SHA256:?workflow env required}"
: "${APPIMAGE_RUNTIME_VERSION:?workflow env required}" "${APPIMAGE_RUNTIME_SHA256:?workflow env required}"
[[ "$APPIMAGETOOL_VERSION" =~ ^[0-9.]+$ && "$APPIMAGE_RUNTIME_VERSION" =~ ^[0-9]+$ ]]
[[ "$APPIMAGETOOL_SHA256" =~ ^[0-9a-f]{64}$ && "$APPIMAGE_RUNTIME_SHA256" =~ ^[0-9a-f]{64}$ ]]
artifact="$output/CoveArchive-$version-x86_64.AppImage"
[[ ! -e "$artifact" ]] || { echo 'Refusing stale artifact overwrite' >&2; exit 1; }
appdir="$work/CoveArchive.AppDir"
mkdir -p "$appdir/usr/lib" "$appdir/usr/bin" "$appdir/usr/share/icons/hicolor/512x512/apps" \
    "$appdir/usr/share/doc/covearchive"
cp -a -- "$input" "$appdir/usr/lib/covearchive"
install -m 755 "$ROOT/packaging/linux/AppRun" "$appdir/AppRun"
install -m 644 "$ROOT/packaging/linux/covearchive.desktop" "$appdir/covearchive.desktop"
install -m 644 "$icon" "$appdir/covearchive.png"
install -m 644 "$icon" "$appdir/usr/share/icons/hicolor/512x512/apps/covearchive.png"
install -m 644 "$ROOT/LICENSE" "$appdir/usr/share/doc/covearchive/copyright"
ln -s ../lib/covearchive/CoveArchive "$appdir/usr/bin/covearchive"
ln -s covearchive.png "$appdir/.DirIcon"
find "$appdir" -type d -exec chmod 755 {} +
find "$appdir" -type f -exec chmod go-w,u-s,g-s {} +
python3 "$ROOT/packaging/verify_package.py" "$appdir" --platform linux
tool="$work/appimagetool.AppImage"
runtime="$work/runtime-x86_64"
# Pins come only from the workflow env; digests are from the GitHub release API.
curl --fail --location --proto '=https' --tlsv1.2 --output "$tool" \
    "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
printf '%s  %s\n' "$APPIMAGETOOL_SHA256" "$tool" | sha256sum -c -
curl --fail --location --proto '=https' --tlsv1.2 --output "$runtime" \
    "https://github.com/AppImage/type2-runtime/releases/download/$APPIMAGE_RUNTIME_VERSION/runtime-x86_64"
printf '%s  %s\n' "$APPIMAGE_RUNTIME_SHA256" "$runtime" | sha256sum -c -
chmod +x "$tool" "$runtime"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$tool" --appimage-extract-and-run \
    --runtime-file "$runtime" "$appdir" "$artifact"
printf '%s\n' "$artifact"
