#!/usr/bin/env bash
set -euo pipefail
umask 022
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
init() {
    [[ $(uname -m) == x86_64 ]] || { echo 'Only x86_64 packages are supported' >&2; return 1; }
    input=$(realpath -- "${1:?pass PyInstaller onedir directory}")
    output=$(realpath -m -- "${2:?pass artifact output directory}")
    [[ -x "$input/CoveArchive" && -d "$input/_internal" ]] || { echo 'Invalid onedir input' >&2; return 1; }
    version=$(python3 -c 'import runpy,sys; print(runpy.run_path(sys.argv[1])["__version__"])' "$ROOT/src/covearchive/__init__.py")
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    mkdir -p -- "$ROOT/build/release/linux" "$output"
    work=$(mktemp -d "$ROOT/build/release/linux/package.XXXXXXXX")
    icon="$ROOT/src/covearchive/assets/covearchive-icon.png"
}
