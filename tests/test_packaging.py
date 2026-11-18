"""Offline package diagnostics and release packaging checks."""

import importlib
import hashlib
import io
import json
import marshal
import os
import re
import shutil
from pathlib import Path
import runpy
import subprocess
import struct
import tarfile
import sys
from unittest.mock import patch
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_package_selftest_real_worker_and_isolated_data(tmp_path):
    # Import first: a missing diagnostic must fail without falling through to GUI startup.
    importlib.import_module("covearchive.ui.package_selftest")
    env = os.environ.copy()
    for key in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        directory = tmp_path / key
        directory.mkdir()
        env[key] = str(directory)
    env["QT_QPA_PLATFORM"] = "offscreen"
    # Unit coverage uses a vault double; the actual diagnostic always requires
    # its native vault check (separately exercised on the built executables).
    script = (
        "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
        "from covearchive.ui import package_selftest as p;"
        "p.check_vault=lambda:'unit_test_vault';"
        "sys.argv=[sys.argv[2],'--package-selftest'];"
        "runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                             str(ROOT / "src"), str(ROOT / "run_covearchive.py")],
                            env=env, capture_output=True, timeout=25)
    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert set(report["checks"]) == {"worker", "cancel", "queue", "vault", "shutdown"}
    assert report["checks"]["cancel"] == "proxy_connected_cancelled_reaped_eof"
    assert report["checks"]["worker"] == "invalid_url_reaped"
    assert report["frozen"] is False
    for key in ("LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        assert not list((tmp_path / key).iterdir())


def test_selftest_restores_environment_on_failure(tmp_path):
    module = importlib.import_module("covearchive.ui.package_selftest")
    before = dict(os.environ)
    try:
        with module.isolated_paths(tmp_path):
            assert Path(os.environ["LOCALAPPDATA"]).is_relative_to(tmp_path)
            assert Path(os.environ["XDG_STATE_HOME"]).is_relative_to(tmp_path)
            raise RuntimeError("intentional")
    except RuntimeError:
        pass
    assert dict(os.environ) == before


def test_frozen_worker_environment_passthrough():
    module = importlib.import_module("covearchive.ui.scan_process")
    diagnostic = importlib.import_module("covearchive.ui.package_selftest")
    app = diagnostic.QApplication.instance() or diagnostic.QApplication([])
    keys = ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC")
    values = {key: str(index) for index, key in enumerate(keys)}
    values.update(TEMP="isolated-temp", GITHUB_TOKEN="filtered", PATH="filtered", _PYI_UNKNOWN="filtered")
    for frozen in (False, True):
        controller = module.ScanController(command_factory=lambda _: [sys.executable])
        with patch.dict(os.environ, values, clear=True), patch.object(sys, "frozen", frozen, create=True), \
                patch.object(module.QProcess, "start", lambda self: None):
            assert controller.start("not-a-repository-url")
            env = controller.job.process.processEnvironment()
            actual = {key: env.value(key) for key in env.keys()}
            assert actual == ({"TEMP": "isolated-temp", **{k: values[k] for k in keys}} if frozen
                              else {"TEMP": "isolated-temp"})
        assert controller.shutdown()
        app.processEvents()


def test_selftest_throwaway_vault_cleanup_after_failed_write():
    module = importlib.import_module("covearchive.ui.package_selftest")
    targets, deleted = [], []

    class Vault:
        def __init__(self, target):
            targets.append(target)

        def load(self):
            return None

        def save(self, token):
            raise RuntimeError("intentional")

        def delete(self):
            deleted.append(True)

    with patch.object(module.sys, "platform", "win32"), patch.object(module, "WindowsCredentialVault", Vault):
        for _ in range(2):
            try:
                module.check_vault()
            except RuntimeError:
                pass
    assert len(set(targets)) == 2
    assert all(t.startswith("CoveArchive/package-selftest/") for t in targets)
    assert deleted == [True, True]


def test_selftest_reports_failure_without_exception_details(tmp_path):
    module = importlib.import_module("covearchive.ui.package_selftest")
    reports = []
    with patch.object(module.tempfile, "tempdir", str(tmp_path)), \
            patch.object(module, "run_checks", side_effect=RuntimeError("private details")), \
            patch.object(module, "emit_result", reports.append):
        assert module.main(ROOT / "run_covearchive.py") == 1
    assert reports == [{"ok": False, "frozen": False, "checks": {}, "error": "RuntimeError"}]
    assert not list(tmp_path.iterdir())


def test_selftest_vault_roundtrip_deletes_only_throwaway_target():
    module = importlib.import_module("covearchive.ui.package_selftest")
    targets, values = [], {}

    class Vault:
        def __init__(self, target):
            self.target = target
            targets.append(target)

        def load(self):
            return values.get(self.target)

        def save(self, token):
            values[self.target] = token

        def delete(self):
            del values[self.target]

    with patch.object(module.sys, "platform", "win32"), patch.object(module, "WindowsCredentialVault", Vault):
        assert module.check_vault() == "throwaway_roundtrip_deleted"
    assert len(targets) == 1 and targets[0] != "CoveArchive/github-token"
    assert not values


def test_windowed_diagnostic_writes_inherited_output_pipe():
    if sys.platform != "win32":
        return
    executable = Path(sys.executable).with_name("pythonw.exe")
    script = ("import sys;sys.path.insert(0,sys.argv[1]);"
              "from covearchive.ui.package_selftest import emit_result;"
              "sys.stdout=None;emit_result({'ok':True})")
    result = subprocess.run([str(executable), "-I", "-B", "-c", script, str(ROOT / "src")],
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def package_fixture(root, linux=False):
    files = ['_internal/covearchive/assets/covearchive-icon.png', 'certifi/cacert.pem']
    files += (['PySide6/plugins/platforms/libqxcb.so', 'PySide6/plugins/platforms/libqoffscreen.so', 'libQt6DBus.so.6', 'libxcb-cursor.so.0']
              if linux else ['PySide6/plugins/platforms/qwindows.dll', 'PySide6/plugins/platforms/qoffscreen.dll'])
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    return root


def test_package_verifier_rejects_planted_data_and_missing_runtime(tmp_path):
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    plants = ['queue.sqlite3', '.tmp-unrelated/data', 'tests/test_data.py', '.git/config', '.env',
              'preferences.json', 'library.json', 'evidence/log', 'private.key', 'private.pem']
    for index, name in enumerate(plants):
        root = package_fixture(tmp_path / str(index)); assert verify(root, 'windows') == []
        planted = root / name
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(b"fixture")
        expected = ['forbidden package path: ' + name]
        if '/' in name: expected.append('forbidden package path: ' + name.split('/')[0])
        assert verify(root, 'windows') == sorted(expected)
        planted.unlink()
        while planted.parent != root:
            planted = planted.parent; planted.rmdir()
        assert verify(root, 'windows') == []
    for index, value in enumerate((str(ROOT).encode(), str(Path.home()).encode(), b'ghp_' + b'A' * 36)):
        root = package_fixture(tmp_path / ('secret' + str(index))); assert verify(root, 'windows') == []
        secret = root / 'leak.txt'
        secret.write_bytes(value)
        kind = 'secret' if index == 2 else 'build-host path'
        assert verify(root, 'windows') == ['forbidden ' + kind + ': leak.txt']
        secret.unlink(); assert verify(root, 'windows') == []
    root = package_fixture(tmp_path / 'missing'); (root / 'PySide6/plugins/platforms/qwindows.dll').unlink()
    assert verify(root, 'windows') == ['missing Qt platform plugin: qwindows.dll']


def test_verifier_inspects_zip_members_and_accepts_empty_namespace(tmp_path):
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    root = package_fixture(tmp_path / 'zip'); archive = root / 'bundle.zip'
    assert verify(root, 'windows') == []
    with zipfile.ZipFile(archive, 'w') as output: output.writestr('private/queue.sqlite3', b'private')
    assert verify(root, 'windows') == ['forbidden package path: bundle.zip/private/queue.sqlite3']
    archive.unlink(); assert verify(root, 'windows') == []
    (root / 'namespace.pyz').write_bytes(b'PYZ\0' + b'\0' * 4 + struct.pack('!I', 12) + marshal.dumps({'namespace': (3, 0, 0)}))
    assert verify(root, 'windows') == []


def test_verifier_build_host_paths_only_in_first_party_content(tmp_path):
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    root = package_fixture(tmp_path / 'upstream')
    for name in ('Qt6Core.dll', 'notes.txt'):
        (root / name).write_bytes(b'C:\\Users\\qt\\work\\Qt6Core.pdb; C:/Users/Barney')
    assert verify(root, 'windows') == []
    for index, path in enumerate((ROOT, ROOT / 'build/release/evidence', Path.home(), Path(os.environ.get('TEMP', str(tmp_path))))):
        root = package_fixture(tmp_path / str(index)); assert verify(root, 'windows') == []
        text = root / 'notes.txt'; text.write_bytes(str(path).encode())
        assert verify(root, 'windows') == ['forbidden build-host path: notes.txt']
        text.unlink(); assert verify(root, 'windows') == []


def check_linux_installation_namespace(tmp_path, prefix):
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    root = tmp_path / 'tree'; runtime = package_fixture(root / prefix, linux=True)
    paths = verify.__globals__['build_host_paths']() | {b'/home/runner'}
    with patch.dict(verify.__globals__, build_host_paths=lambda: paths):
        assert verify(root, 'linux') == []
        binary = runtime / '_internal/libpython3.12.so.1.0'
        binary.write_bytes(b'upstream build: /home/runner/work/cpython/Modules/example.c')
        assert verify(root, 'linux') == []
        package = runtime / '_internal/covearchive/fixture.py'
        package.parent.mkdir(parents=True, exist_ok=True)
        for value, kind in ((str(ROOT).encode(), 'build-host path'),
                            (str(Path.home()).encode(), 'build-host path'),
                            (b'ghp_' + b'A' * 36, 'secret')):
            package.write_bytes(value)
            assert verify(root, 'linux') == ['forbidden ' + kind + ': ' + package.relative_to(root).as_posix()]
            package.unlink(); assert verify(root, 'linux') == []
        metadata = runtime / 'runtime.json'; metadata.write_bytes(str(ROOT).encode())
        assert verify(root, 'linux') == ['forbidden build-host path: ' + metadata.relative_to(root).as_posix()]
        metadata.unlink(); assert verify(root, 'linux') == []
        binary.write_bytes(b'ghp_' + b'A' * 36)
        assert verify(root, 'linux') == ['forbidden secret: ' + binary.relative_to(root).as_posix()]
        binary.unlink(); assert verify(root, 'linux') == []


def test_deb_installation_prefix_is_not_python_namespace(tmp_path):
    check_linux_installation_namespace(tmp_path, 'opt/covearchive')


def test_appimage_installation_prefix_is_not_python_namespace(tmp_path):
    check_linux_installation_namespace(tmp_path, 'usr/lib/covearchive')


def test_onedir_root_is_not_python_namespace(tmp_path):
    check_linux_installation_namespace(tmp_path, 'CoveArchive')


def test_verifier_distinguishes_parser_markers_from_private_key_material(tmp_path):
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    root = package_fixture(tmp_path / 'key'); binary = root / 'tls-backend.dll'
    binary.write_bytes(b'parser marker: -----BEGIN PRIVATE KEY-----\0')
    assert verify(root, 'windows') == []
    binary.write_bytes(b'-----BEGIN PRIVATE KEY-----\n' + b'M' * 64 + b'\n-----END PRIVATE KEY-----')
    assert verify(root, 'windows') == ['forbidden secret: tls-backend.dll']
    binary.unlink(); assert verify(root, 'windows') == []


def test_build_lock_is_approved_and_every_requirement_has_hashes():
    data = (ROOT / "packaging/requirements-build.txt").read_bytes()
    # Git's existing eol=lf policy normalizes CRLF checkout bytes.
    # Pin the same content across both line-ending formats.
    assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == "729756d491e839c62554751e5250bdef25241dcfaeeced4e85fb66eb12b1f2cf"
    entries = [line.strip() for line in data.decode().replace("\r\n", "\n").replace("\\\n", " ").splitlines()
               if line.strip() and not line.startswith("#")]
    assert len(entries) == 21
    for entry in entries:
        assert re.match(r"^[A-Za-z0-9_-]+==[0-9][^\s;]*", entry)
        assert re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", entry)
        assert not re.search(r"https?://|^-|>=|<=", entry)


def test_installer_is_per_user_and_cannot_remove_user_data():
    script = (ROOT / "packaging/windows.iss").read_text()
    assert "PrivilegesRequired=lowest" in script
    assert "PrivilegesRequiredOverridesAllowed" not in script
    assert "DefaultDirName={autopf}\\CoveArchive" in script
    assert "AppVersion={#AppVersion}" in script and "#error" in script
    assert 'AppSource "dist\\CoveArchive"' in script
    assert 'Source: "{#AppSource}\\*"' in script
    assert "OutputBaseFilename=CoveArchive-{#AppVersion}-Setup" in script
    assert "LicenseFile=LICENSE" in script and "Flags: unchecked" in script
    assert re.search(r"AppId=\{\{[A-F0-9-]{36}\}", script)
    assert all(word not in script.lower() for word in
               ("{localappdata}", "{userappdata}", "[registry]", "[run]", "autostart", "serviceinstall"))
    assert "[UninstallDelete]" not in script
    assert 'OutputDir={#OutputDir}' in script and 'MinVersion=10.0' in script


def test_windows_acceptance_oracles():
    installer = (ROOT / 'packaging/windows.iss').read_text()
    assert 'VersionInfoVersion={#AppVersion}' in installer


def test_windows_ci_installation_oracles():
    script = (ROOT / 'packaging/windows_ci.ps1').read_text()
    for executable in ('$portable', '"$onedir/CoveArchive.exe"', '$setup'):
        assert 'Test-BinaryVersion ' + executable in script
    assert 'VersionInfo' in script and '.FileVersion' in script and '.ProductVersion.Trim()' in script
    assert '@($version, "$version.0")' in script
    assert 'RegistryHive]::CurrentUser' in script and 'RegistryHive]::LocalMachine' in script
    assert 'RegistryView]::Registry32' in script and 'RegistryView]::Registry64' in script
    assert 'Test-UninstallRegistration $true' in script and script.count('Test-UninstallRegistration $false') == 2
    installed = script.index("throw 'Silent per-user install failed'")
    removed = script.index("throw 'Silent uninstall failed'")
    assert installed < script.index('Test-UninstallRegistration $true') < removed
    assert script.rindex('Test-UninstallRegistration $false') > removed
    assert 'python packaging/verify_package.py $installDir --platform windows' in script
    assert "[Environment]::GetFolderPath('LocalApplicationData')" in script
    assert "$sentinelDir = Join-Path $dataRoot ('packaging-' + [Guid]::NewGuid().ToString('N'))" in script
    assert 'finally {' in script and 'Remove-Item -LiteralPath $sentinelDir' in script


def run_powershell(script, env=None):
    executable = shutil.which('powershell') or shutil.which('pwsh')
    if executable is None:
        pytest.skip('PowerShell is not installed')
    return subprocess.run([executable, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                          env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize('actual,pin,parts,accepted', [
    ('6.7.1', '6.7.1', (6, 7, 1), True),
    ('6.7.1.0', '6.7.1', (6, 7, 1), True),
    (' \t6.7.1.0\r\n', '6.7.1', (6, 7, 1), True),
    ('6.7.1.42', '6.7.1', (6, 7, 1), True),
    ('7.7.1', '6.7.1', (7, 7, 1), False),
    ('6.8.1', '6.7.1', (6, 8, 1), False),
    ('6.7.2', '6.7.1', (6, 7, 2), False),
    ('6.7.10', '6.7.1', (6, 7, 10), False),
    ('6.7', '6.7.1', (6, 7, 1), False),
    ('6.7.1.beta', '6.7.1', (6, 7, 1), False),
    ('6.7.1-beta', '6.7.1', (6, 7, 1), False),
    ('6.7.1 (a)', '6.7.1', (6, 7, 1), False),
    ('06.7.1', '6.7.1', (6, 7, 1), False),
    ('6.07.1', '6.7.1', (6, 7, 1), False),
    ('6.7.01', '6.7.1', (6, 7, 1), False),
    ('6.7.1.00', '6.7.1', (6, 7, 1), False),
    ('', '6.7.1', (6, 7, 1), False),
    (None, '6.7.1', (6, 7, 1), False),
    ('6.7.1', '6.7', (6, 7, 1), False),
    ('6.7.1', '6.7.1.0', (6, 7, 1), False),
    ('6.7.1', '6.7.1-beta', (6, 7, 1), False),
    ('6.7.1', '06.7.1', (6, 7, 1), False),
    ('6.7.1', '6.7.1\n', (6, 7, 1), False),
    ('6.7.1', '', (6, 7, 1), False),
    ('6.7.1', '6.7.1', (7, 7, 1), False),
    ('6.7.1', '6.7.1', (6, 8, 1), False),
    ('6.7.1', '6.7.1', (6, 7, 2), False),
])
def test_windows_inno_version(actual, pin, parts, accepted):
    env = os.environ.copy()
    env['INNO_TEST_HELPER'] = str(ROOT / 'packaging/windows_inno_version.ps1')
    env['INNO_TEST_CASE'] = json.dumps(dict(ProductVersion=actual, FileVersion='diagnostic-file-version',
        ProductMajorPart=parts[0], ProductMinorPart=parts[1], ProductBuildPart=parts[2], pin=pin))
    result = run_powershell('''
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
. $env:INNO_TEST_HELPER
$case = $env:INNO_TEST_CASE | ConvertFrom-Json
try { Assert-InnoVersion $case $case.pin; 'accepted' }
catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
''', env)
    assert (result.returncode == 0) == accepted, (result.stdout, result.stderr)
    if accepted:
        assert result.stdout.strip() == 'accepted'
    else:
        assert 'ProductVersion=<' + (actual or '') + '>' in result.stderr
        assert 'FileVersion=<diagnostic-file-version>' in result.stderr
        assert 'pinned=<' + pin + '>' in result.stderr
        if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', pin):
            assert 'Invalid pinned Inno version' in result.stderr


def test_windows_ci_enforces_inno_version():
    script = (ROOT / 'packaging/windows_ci.ps1').read_text()
    assert not re.search(r'-ne\s+\$env:INNO_VERSION\b', script, re.IGNORECASE)
    assert '. "$PSScriptRoot/windows_inno_version.ps1"' in script
    assert 'Assert-InnoVersion (Get-Item -LiteralPath $compiler).VersionInfo $env:INNO_VERSION' in script
    assert script.index('Assert-InnoVersion ') < script.index('python -m pytest')
    assert "$ErrorActionPreference = 'Stop'" in script
    assert '$PSNativeCommandUseErrorActionPreference = $true' in script


def test_packaging_powershell_parses():
    env = os.environ.copy()
    env['PACKAGING_PS_ROOT'] = str(ROOT / 'packaging')
    result = run_powershell('''
$ErrorActionPreference = 'Stop'
Get-ChildItem -LiteralPath $env:PACKAGING_PS_ROOT -Filter '*.ps1' | ForEach-Object {
    $tokens = $null; $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
    $_.Name
}
''', env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert set(result.stdout.split()) == {path.name for path in (ROOT / 'packaging').glob('*.ps1')}


def test_windows_icon_has_all_required_sizes():
    data = (ROOT / 'packaging/covearchive.ico').read_bytes()
    assert struct.unpack_from('<HHH', data) == (0, 1, 6)
    sizes, end = [], 6 + 6 * 16
    for index in range(6):
        width, height, colors, reserved, planes, bits, length, offset = struct.unpack_from('<BBBBHHII', data, 6 + index * 16)
        size = width or 256; sizes.append(size)
        assert (height or 256) == size and (colors, reserved, planes, bits) == (0, 0, 1, 32)
        assert offset == end
        png = data[offset:offset + length]
        assert png[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack_from('>II', png, 16) == (size, size)
        end += length
    assert sizes == [16, 24, 32, 48, 64, 256] and end == len(data)


def test_linux_packaging_metadata_and_download_safety():
    folder = ROOT / "packaging/linux"
    desktop = (folder / "covearchive.desktop").read_text()
    assert all(line in desktop.splitlines() for line in
               ("Type=Application", "Name=CoveArchive", "Exec=covearchive", "Icon=covearchive", "Terminal=false"))
    control = (folder / "control.in").read_text()
    assert all(line in control.splitlines() for line in
               ("Package: covearchive", "Version: @VERSION@", "Architecture: amd64", "Recommends: git"))
    for library in ("libc6 (>= 2.35)", "libdbus-1-3", "libxkbcommon-x11-0", "libxcb-cursor0", "libgl1"):
        assert library in control
    for name in ("build-appimage.sh", "build-deb.sh"):
        script = (folder / name).read_text()
        assert "set -euo pipefail" in script
        assert "sudo" not in script and "rm -rf" not in script
    appimage = (folder / "build-appimage.sh").read_text()
    assert "--runtime-file" in appimage and "--appimage-extract-and-run" in appimage
    assert "curl --fail --location --proto '=https' --tlsv1.2" in appimage
    assert appimage.index("sha256sum -c") < appimage.index("chmod +x")
    assert "APPIMAGETOOL_SHA256:?" in appimage and "APPIMAGE_RUNTIME_SHA256:?" in appimage
    deb = (folder / "build-deb.sh").read_text()
    assert "dpkg-deb --build --root-owner-group" in deb
    assert '"$stage/opt/covearchive"' in deb
    assert 'ln -s ../../opt/covearchive/CoveArchive' in deb
    assert "postinst" not in deb and "chmod -R 777" not in deb


def test_public_notes_and_debian_policy_metadata():
    notes = (ROOT / 'packaging/release-notes.md').read_text()
    assert 'Windows 10 or newer' in notes
    assert 'Planned artifacts' not in notes and 'not available' not in notes
    control = (ROOT / 'packaging/linux/control.in').read_text()
    assert 'Maintainer: Cove <noreply@covearchive.invalid>' in control
    assert 'Homepage: https://github.com/Sin213/CoveArchive' in control


def test_linux_verifier_requires_bundled_cursor_and_xcb(tmp_path):
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    names = ['covearchive/assets/covearchive-icon.png', 'certifi/cacert.pem',
             'PySide6/plugins/platforms/libqxcb.so', 'PySide6/plugins/platforms/libqoffscreen.so',
             'libQt6DBus.so.6']
    for name in names:
        path = tmp_path / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
    assert any('libxcb-cursor.so.0' in issue for issue in verify(tmp_path, 'linux'))
    (tmp_path / 'libxcb-cursor.so.0').write_bytes(b'fixture')
    assert verify(tmp_path, 'linux') == []
    (tmp_path / names[2]).unlink()
    assert any('libqxcb.so' in issue for issue in verify(tmp_path, 'linux'))


def test_workflow_security_contract():
    workflow = (ROOT / '.github/workflows/release.yml').read_text()
    assert 'workflow_dispatch:' in workflow and 'expected_commit:' in workflow
    assert 'permissions: {}' in workflow and workflow.count('contents: write') == 1
    assert all(bad not in workflow for bad in ('pull_request_target', 'push:', 'schedule:', '-latest', '--latest', 'draft=false', 'gh release edit'))
    uses = re.findall(r'uses:\s+(\S+)', workflow)
    assert uses and all(re.fullmatch(r'[^@]+@[a-f0-9]{40}', use) for use in uses)
    assert 'persist-credentials: false' in workflow
    assert 'PYTHON_VERSION: 3.12.10' in workflow
    assert 'python -m pip install --require-hashes --no-deps -r packaging/requirements-build.txt' in workflow
    linux_ci = (ROOT / 'packaging/linux/ci.sh').read_text()
    assert 'bash packaging/linux/ci.sh' in workflow
    assert 'ubuntu:22.04' in linux_ci and 'xvfb-run' in linux_ci
    triggers = workflow.split('on:\n', 1)[1].split('permissions:', 1)[0]
    assert re.findall(r'^  ([a-z_]+):', triggers, re.M) == ['workflow_dispatch']
    pins = {'checkout': '3d3c42e5aac5ba805825da76410c181273ba90b1',
            'setup-python': '6d4fd22b20465ccc0f7354476588e8df7c9ca0e0',
            'upload-artifact': 'cf430e030ddbb5b0abf93d22962f4752f3646cd9',
            'download-artifact': '9000827ccba6bdab643e8b6fd33ac0654aef8333'}
    for use in uses:
        action, sha = use.split('@'); assert sha == pins[action.removeprefix('actions/')]
    job_chunks = re.split(r'^  ([a-z-]+):\n', workflow.split('jobs:\n', 1)[1], flags=re.M)
    assert job_chunks[1::2] == ['preflight', 'build-windows', 'build-linux', 'draft']
    for name, body in zip(job_chunks[1::2], job_chunks[2::2]):
        assert 'timeout-minutes:' in body
        assert 'contents: ' + ('write' if name == 'draft' else 'read') in body
        assert re.search(r'runs-on: (windows-2022|ubuntu-22.04)', body)
    in_run = False; run_indent = 0
    for line in workflow.splitlines():
        indent = len(line) - len(line.lstrip())
        if in_run and line.strip() and indent <= run_indent: in_run = False
        if in_run: assert '${{' not in line
        if re.search(r'run:\s*\|', line): in_run = True; run_indent = indent
    assert set(re.findall(r'secrets\.([A-Za-z_]+)', workflow)) <= {'GITHUB_TOKEN'}
    release = (ROOT / 'packaging/draft_release.py').read_text()
    assert '"--draft"' in release and '--clobber' not in release
    assert all(bad not in release for bad in ('--latest', 'draft=false', 'release", "edit', '--force'))


def test_draft_receipt_refuses_every_mismatch(tmp_path):
    import copy
    module = runpy.run_path(str(ROOT / 'packaging/draft_release.py'))
    expected = {'asset.exe': {'size': 3, 'sha256': 'a' * 64}}
    good = {'tag_name': 'v1.0.0', 'draft': True, 'target_commitish': 'b' * 40,
            'html_url': 'https://example.invalid/draft',
            'assets': [{'name': 'asset.exe', 'size': 3, 'sha256': 'a' * 64}]}
    receipt = tmp_path / 'receipt.json'
    module['verify_release'](good, expected, 'b' * 40, False, receipt)
    assert json.loads(receipt.read_text())['commit'] == 'b' * 40
    variants = []
    for field, value in [('target_commitish', 'c' * 40), ('draft', False), ('assets', [])]:
        bad = copy.deepcopy(good); bad[field] = value; variants.append(bad)
    bad = copy.deepcopy(good); bad['assets'] *= 2; variants.append(bad)
    bad = copy.deepcopy(good); bad['assets'].append({'name': 'extra.exe', 'size': 1, 'sha256': 'c' * 64}); variants.append(bad)
    for field, value in [('size', 4), ('sha256', 'c' * 64), ('name', 'extra.exe')]:
        bad = copy.deepcopy(good); bad['assets'][0][field] = value; variants.append(bad)
    for bad, tag in [(item, False) for item in variants] + [(good, True)]:
        try: module['verify_release'](bad, expected, 'b' * 40, tag)
        except ValueError: pass
        else: raise AssertionError('unsafe draft accepted')


def test_release_checksum_handoff_detects_swapped_artifact(tmp_path):
    module = runpy.run_path(str(ROOT / 'packaging/draft_release.py'))
    rows = []
    for name in sorted(module['NAMES']):
        data = name.encode(); (tmp_path / name).write_bytes(data)
        rows.append(hashlib.sha256(data).hexdigest() + '  ' + name + '\n')
    (tmp_path / 'windows.sha256').write_text(''.join(rows[:2]))
    (tmp_path / 'linux.sha256').write_text(''.join(rows[2:]))
    outputs = {'windows': ''.join(rows[:2]), 'linux': ''.join(rows[2:])}
    assert len(module['checksums'](tmp_path, outputs)) == 5
    (tmp_path / 'SHA256SUMS.txt').unlink()
    (tmp_path / sorted(module['NAMES'])[0]).write_bytes(b'swapped')
    try: module['checksums'](tmp_path, outputs)
    except ValueError as exc: assert 'checksum mismatch' in str(exc)
    else: raise AssertionError('swapped artifact accepted')


def test_release_integrity_requires_independent_job_outputs(tmp_path):
    module = runpy.run_path(str(ROOT / 'packaging/draft_release.py'))
    rows = {label: [] for label in ('windows', 'linux')}
    for name in sorted(module['NAMES']):
        data = name.encode(); (tmp_path / name).write_bytes(data)
        label = 'windows' if name.endswith('.exe') else 'linux'
        rows[label].append(hashlib.sha256(data).hexdigest() + '  ' + name + '\n')
    outputs = {k: ''.join(v) for k, v in rows.items()}
    for label, text in outputs.items(): (tmp_path / (label + '.sha256')).write_text(text)
    for mutation in ('file', 'sidecar', 'output'):
        name = sorted(module['NAMES'])[0]; original = (tmp_path / name).read_bytes()
        sidecar = (tmp_path / 'windows.sha256').read_text(); changed = dict(outputs)
        if mutation == 'file': (tmp_path / name).write_bytes(b'tampered')
        if mutation == 'sidecar': (tmp_path / 'windows.sha256').write_text(sidecar.replace(sidecar[:64], '0' * 64))
        if mutation == 'output': changed['windows'] = sidecar.replace(sidecar[:64], '0' * 64)
        try:
            try: module['checksums'](tmp_path, changed)
            except ValueError: pass
            else: raise AssertionError('tampering accepted: ' + mutation)
        finally:
            (tmp_path / name).write_bytes(original); (tmp_path / 'windows.sha256').write_text(sidecar)
    try: module['checksums'](tmp_path, {})
    except ValueError: pass
    else: raise AssertionError('missing job outputs accepted')


def test_product_paths_platform_and_integrity_channels():
    paths = set(subprocess.run(['git', 'ls-files', '-z'], check=True, capture_output=True).stdout.decode().split('\0'))
    paths.update(p.relative_to(ROOT).as_posix() for folder in ('packaging', '.github', 'src', 'tests')
                 for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for name in paths:
        path = ROOT / name
        if name and path.is_file(): assert (b'ca' + b'-r1') not in path.read_bytes(), name
    workflow = (ROOT / '.github/workflows/release.yml').read_text()
    assert workflow.count('timeout-minutes: 45') == 2
    assert 'GH_REPO: ${{ github.repository }}' in workflow
    for label in ('windows', 'linux'):
        assert 'needs.build-' + label + '.outputs.sha256' in workflow
        assert 'sha256: ${{ steps.build.outputs.sha256 }}' in workflow
    windows = (ROOT / 'packaging/windows_ci.ps1').read_text()
    assert "Remove-Item Env:QT_QPA_PLATFORM" in windows
    assert windows.index('Remove-Item Env:QT_QPA_PLATFORM') < windows.index('function Test-Application')


def test_version_metadata_and_artifact_contract():
    from covearchive import __version__
    spec = (ROOT / 'packaging/covearchive.spec').read_text()
    installer = (ROOT / 'packaging/windows.iss').read_text()
    assert 'version = runpy.run_path' in spec and '["__version__"]' in spec
    assert 'AppVersion={#AppVersion}' in installer
    names = runpy.run_path(str(ROOT / 'packaging/draft_release.py'))['NAMES']
    assert len(names) == 4 and all(__version__ in name for name in names)
    notes = (ROOT / 'packaging/release-notes.md').read_text()
    assert all(name in notes for name in names)


def test_release_notes_document_unsigned_artifacts_without_announcing_downloads():
    notes = (ROOT / 'packaging/release-notes.md').read_text()
    assert 'All binaries are unsigned' in notes
    assert 'not a code-signing signature' in notes
    assert 'Windows requires 64-bit Windows 10 or newer' in notes
    assert 'glibc >= 2.35' in notes
    assert 'Git 2.45 or newer' in notes and 'auto-update' in notes
    assert 'available now' not in notes and 'download now' not in notes.lower()


def test_preflight_refuses_wrong_commit_branch_or_version(tmp_path):
    base = dict(EXPECTED_COMMIT='a' * 40, GITHUB_SHA='a' * 40,
                GITHUB_REF='refs/heads/main', DEFAULT_BRANCH='main')
    for changed in ({'EXPECTED_COMMIT': 'malformed'}, {'GITHUB_SHA': 'b' * 40}, {'GITHUB_REF': 'refs/heads/other'}):
        with patch.dict(os.environ, base | changed):
            try: runpy.run_path(str(ROOT / 'packaging/preflight.py'))
            except SystemExit: pass
            else: raise AssertionError('unsafe preflight accepted')
    with patch.dict(os.environ, base):
        runpy.run_path(str(ROOT / 'packaging/preflight.py'))
    source = tmp_path / 'src/covearchive'; source.mkdir(parents=True)
    (source / '__init__.py').write_text('__version__="0.0.0"\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="0.0.0"\n')
    previous = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch.dict(os.environ, base):
            try: runpy.run_path(str(ROOT / 'packaging/preflight.py'))
            except SystemExit: pass
            else: raise AssertionError('wrong source version accepted')
    finally: os.chdir(previous)


def test_draft_orchestration_never_replaces_assets(tmp_path):
    import copy
    fn = runpy.run_path(str(ROOT / 'packaging/draft_release.py'))['create_or_reuse_draft']
    digest = hashlib.sha256(b'abc').hexdigest(); expected = {'asset.exe': {'size': 3, 'sha256': digest}}
    for mode in ('reused', 'fresh', 'race'):
        case = tmp_path / mode; (case / 'dist').mkdir(parents=True); calls = []
        state = dict(id=1, tag_name='v1.0.0', draft=True, target_commitish='a' * 40, html_url='https://example.invalid/draft', assets=[])
        exists = mode == 'reused'
        if exists: state['assets'] = [dict(id=7, name='asset.exe', size=3)]
        def fake_api(endpoint):
            if '/git/matching-refs/' in endpoint: return []
            assert endpoint.endswith('/releases/1'); return copy.deepcopy(state)
        def fake_command(*args):
            nonlocal exists
            calls.append(args)
            if args[1:3] == ('release', 'create'): exists = True; return b''
            if args[1:3] == ('release', 'upload'):
                state['assets'] = [dict(id=7, name='asset.exe', size=3)]
                if mode == 'race': state['draft'] = False
                return b''
            if '/releases/assets/' in args[2]: return b'abc'
            assert args[1] == 'api' and '--paginate' in args
            return json.dumps([[copy.deepcopy(state)] if exists else []]).encode()
        previous = Path.cwd(); error = None
        try:
            os.chdir(case)
            with patch.dict(fn.__globals__, checksums=lambda *a: expected, command=fake_command, api=fake_api), \
                    patch.dict(os.environ, GITHUB_REPOSITORY='owner/repo', GITHUB_SHA='a' * 40, GITHUB_STEP_SUMMARY=str(case / 'summary')):
                try: fn(case)
                except ValueError as exc: error = exc
        finally: os.chdir(previous)
        uploads = [call for call in calls if call[1:3] == ('release', 'upload')]
        assert all(not set(call).intersection(('--clobber', 'edit', 'delete', '--latest'))
                   and not any('draft=false' in argument for argument in call) for call in calls)
        if mode == 'reused': assert error and 'delete the draft and rerun' in str(error) and not uploads
        elif mode == 'fresh': assert error is None and len(uploads) == 1 and (case / 'dist/release-receipt.json').is_file()
        else: assert error and 'draft' in str(error) and len(uploads) == 1


def synthetic_carchive(value='safe', forbidden=False):
    import zlib
    raw = zlib.compress(marshal.dumps(compile('value=' + repr(value), 'covearchive/fixture.py', 'exec')))
    pyz = b'PYZ\0' + b'\0' * 4 + struct.pack('!I', 12 + len(raw)) + raw + marshal.dumps({'covearchive.fixture': (0, 12, len(raw))})
    members = [('PYZ.pyz', pyz)] + ([('covearchive/queue.sqlite3', b'private')] if forbidden else [])
    body = b''; toc = b''
    for name, data in members:
        packed = zlib.compress(data); encoded = name.encode() + b'\0'
        toc += struct.pack('!IIIIBc', 18 + len(encoded), len(body), len(packed), len(data), 1, b'z') + encoded
        body += packed
    return b'\x7fELFdummy' + body + toc + struct.pack('!8sIIII64s', b'MEI\014\013\012\013\016', len(body) + len(toc) + 88, len(body), len(toc), 312, b'python312.so')


def test_extensionless_carchive_directory_and_deb_inspection(tmp_path):
    from types import SimpleNamespace
    verify = runpy.run_path(str(ROOT / 'packaging/verify_package.py'))['verify']
    root = package_fixture(tmp_path / 'tree', linux=True); binary = root / 'CoveArchive'
    def inspect_both(data):
        binary.write_bytes(data); direct = verify(root, 'linux'); stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for path in root.rglob('*'):
                if path.is_file(): archive.add(path, arcname=path.relative_to(root).as_posix())
        with patch('subprocess.run', return_value=SimpleNamespace(stdout=stream.getvalue())):
            assert verify(tmp_path / 'fixture.deb', 'linux') == direct
        return direct
    assert inspect_both(synthetic_carchive()) == []
    for value, forbidden, diagnostic in ((str(ROOT), False, 'forbidden build-host path: CoveArchive/PYZ.pyz/covearchive.fixture'),
            ('ghp_' + 'A' * 36, False, 'forbidden secret: CoveArchive/PYZ.pyz/covearchive.fixture'),
            ('safe', True, 'forbidden package path: CoveArchive/covearchive/queue.sqlite3')):
        assert inspect_both(synthetic_carchive(value, forbidden)) == [diagnostic]
        assert inspect_both(synthetic_carchive()) == []


def test_verification_container_is_digest_pinned():
    script = (ROOT / 'packaging/linux/ci.sh').read_text()
    images = [line.split(' bash ', 1)[0].split()[-1] for line in script.splitlines() if line.startswith('docker run ')]
    assert images and all(re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}', image) for image in images)
    assert images == ['ubuntu:22.04@sha256:9abcaffc92a5279fff3a6a41635c0800ea33e528425fb389658806476e7defde']
