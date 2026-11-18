"""PyInstaller 6.21: onedir by default; Windows COVEARCHIVE_ONEFILE=1 for Portable."""
import os
from pathlib import Path
import runpy
import sys

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / "src"))
version = runpy.run_path(str(root / "src/covearchive/__init__.py"))["__version__"]
windows = sys.platform == "win32"
onefile = windows and os.environ.get("COVEARCHIVE_ONEFILE") == "1"
name = f"CoveArchive-{version}-Portable" if onefile else "CoveArchive"
resource = None
if windows:
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct,
    )
    numbers = tuple(map(int, version.split("."))) + (0,)
    resource = VSVersionInfo(ffi=FixedFileInfo(filevers=numbers, prodvers=numbers,
        mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)), kids=[
        StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in {
            "FileDescription": "CoveArchive", "FileVersion": version,
            "ProductName": "CoveArchive", "ProductVersion": version,
            "OriginalFilename": name + ".exe", "LegalCopyright": "Copyright (c) 2026 Cove",
        }.items()])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])])

desktop_libs = []
if not windows:
    # Force portable desktop support into the bundle, without overriding the
    # exclusion policy for libc / GL / EGL / libstdc++ host libraries.
    for pattern in ("libxcb*.so.*", "libxkbcommon*.so.*", "libdbus-1.so.*"):
        desktop_libs.extend((str(p), ".") for p in Path("/usr/lib/x86_64-linux-gnu").glob(pattern))
a = Analysis([str(root / "run_covearchive.py")], pathex=[str(root / "src")],
    binaries=desktop_libs, datas=collect_data_files("covearchive") + collect_data_files("certifi"),
    hiddenimports=[] if windows else ["PySide6.QtDBus"], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
options = dict(name=name, console=not windows, debug=False, strip=False, upx=False,
               icon=str(root / "packaging/covearchive.ico") if windows else None,
               version=resource, disable_windowed_traceback=True)
if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **options)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **options)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=name)
