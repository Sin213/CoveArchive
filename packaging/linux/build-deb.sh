#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
init "$@"
artifact="$output/covearchive_${version}_amd64.deb"
[[ ! -e "$artifact" ]] || { echo 'Refusing stale artifact overwrite' >&2; exit 1; }
stage="$work/deb"
mkdir -p "$stage/DEBIAN" "$stage/opt" "$stage/usr/bin" \
    "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/512x512/apps" \
    "$stage/usr/share/doc/covearchive"
cp -a -- "$input" "$stage/opt/covearchive"
ln -s ../../opt/covearchive/CoveArchive "$stage/usr/bin/covearchive"
install -m 644 "$ROOT/packaging/linux/covearchive.desktop" "$stage/usr/share/applications/covearchive.desktop"
install -m 644 "$icon" "$stage/usr/share/icons/hicolor/512x512/apps/covearchive.png"
install -m 644 "$ROOT/LICENSE" "$stage/usr/share/doc/covearchive/copyright"
sed "s/@VERSION@/$version/" "$ROOT/packaging/linux/control.in" > "$stage/DEBIAN/control"
find "$stage" -type d -exec chmod 755 {} +
find "$stage" -type f -exec chmod go-w,u-s,g-s {} +
chmod 644 "$stage/DEBIAN/control"
python3 "$ROOT/packaging/verify_package.py" "$stage" --platform linux
dpkg-deb --build --root-owner-group "$stage" "$artifact"
python3 "$ROOT/packaging/verify_package.py" "$artifact" --platform linux
printf '%s\n' "$artifact"
