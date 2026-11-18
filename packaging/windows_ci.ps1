$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$scratch = Join-Path $PWD 'build/release'
$artifacts = Join-Path $PWD 'dist/release'
New-Item -ItemType Directory -Force "$scratch/tmp", $artifacts, "$scratch/results" | Out-Null
$env:TEMP = "$scratch/tmp"; $env:TMP = $env:TEMP
$env:LOCALAPPDATA = "$scratch/user-data"
$env:PYINSTALLER_CONFIG_DIR = "$scratch/pyinstaller-cache"
$env:QT_QPA_PLATFORM = 'offscreen'
$compiler = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
if (!(Test-Path -LiteralPath $compiler)) { throw 'Pinned Inno compiler missing' }
. "$PSScriptRoot/windows_inno_version.ps1"
Assert-InnoVersion (Get-Item -LiteralPath $compiler).VersionInfo $env:INNO_VERSION
python -m pytest
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$version = python -c 'import runpy;print(runpy.run_path("src/covearchive/__init__.py")["__version__"])'
function Test-BinaryVersion([string]$Executable) {
    $info = (Get-Item -LiteralPath $Executable).VersionInfo
    foreach ($value in @($info.FileVersion, $info.ProductVersion.Trim())) {
        if ($value.Trim() -notin @($version, "$version.0")) { throw "Binary version mismatch: $Executable" }
    }
}
function Test-UninstallRegistration([bool]$Installed) {
    $path = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{963CDB21-8306-4E45-B4C9-EEC4E2337AC2}_is1'
    foreach ($pair in @(
        @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64, $Installed),
        @([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64, $false),
        @([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry32, $false))) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($pair[0], $pair[1])
        $key = $null
        try {
            $key = $base.OpenSubKey($path)
            if (($null -ne $key) -ne $pair[2]) { throw 'Unexpected uninstall registration' }
        } finally {
            if ($null -ne $key) { $key.Dispose() }
            $base.Dispose()
        }
    }
}
python -m PyInstaller --noconfirm --workpath build/release/work-onedir --distpath build/release/dist-onedir packaging/covearchive.spec
$onedir = "$scratch/dist-onedir/CoveArchive"
Test-BinaryVersion "$onedir/CoveArchive.exe"
python packaging/verify_package.py $onedir --platform windows
function Test-Application([string]$Executable, [string]$Label) {
    foreach ($flag in @('--smoke-test', '--package-selftest')) {
        $output = "$scratch/results/$Label-$($flag.Substring(2)).stdout"
        $process = Start-Process -FilePath $Executable -ArgumentList $flag -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $output -RedirectStandardError "$output.stderr"
        if ($process.ExitCode -ne 0) { throw "$Label $flag failed: $($process.ExitCode)" }
        if ($flag -eq '--package-selftest') {
            $report = Get-Content -Raw -LiteralPath $output | ConvertFrom-Json
            if (!$report.ok -or !$report.frozen) { throw 'Invalid frozen selftest receipt' }
        }
    }
}
Test-Application "$onedir/CoveArchive.exe" 'onedir'
$env:COVEARCHIVE_ONEFILE = '1'
python -m PyInstaller --noconfirm --workpath build/release/work-onefile --distpath build/release/dist-onefile packaging/covearchive.spec
$portable = "$scratch/dist-onefile/CoveArchive-$version-Portable.exe"
Test-BinaryVersion $portable
python packaging/verify_package.py $portable --platform windows
Test-Application $portable 'onefile'
& $compiler "/DAppVersion=$version" "/DAppSource=$onedir" "/DOutputDir=$artifacts" packaging/windows.iss
$setup = "$artifacts/CoveArchive-$version-Setup.exe"
Test-BinaryVersion $setup
$installDir = Join-Path $env:RUNNER_TEMP 'CoveArchive-install'
if (Test-Path -LiteralPath $installDir) { throw 'Install test directory already exists' }
$dataRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CoveArchive'
$createdDataRoot = !(Test-Path -LiteralPath $dataRoot)
$sentinelDir = Join-Path $dataRoot ('packaging-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $sentinelDir | Out-Null
$sentinel = Join-Path $sentinelDir 'sentinel.txt'
try {
Set-Content -LiteralPath $sentinel -Value 'preserve-user-data'
$before = (Get-FileHash -Algorithm SHA256 -LiteralPath $sentinel).Hash
Test-UninstallRegistration $false
$install = Start-Process -FilePath $setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$installDir`"") -WindowStyle Hidden -Wait -PassThru
if ($install.ExitCode -ne 0) { throw 'Silent per-user install failed' }
Test-UninstallRegistration $true
python packaging/verify_package.py $installDir --platform windows
Test-Application "$installDir/CoveArchive.exe" 'installed'
$uninstall = Start-Process -FilePath "$installDir/unins000.exe" -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -WindowStyle Hidden -Wait -PassThru
if ($uninstall.ExitCode -ne 0) { throw 'Silent uninstall failed' }
Test-UninstallRegistration $false
for ($i=0; $i -lt 50 -and (Test-Path -LiteralPath $installDir); $i++) { Start-Sleep -Milliseconds 100 }
if (Test-Path -LiteralPath $installDir) { throw 'Installation remains after uninstall' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $sentinel).Hash -ne $before) { throw 'User-data sentinel changed' }
if (@(Get-ChildItem -LiteralPath $sentinelDir).Count -ne 1) { throw 'Unexpected user-data changes' }
} finally {
    if (Test-Path -LiteralPath $sentinel) { Remove-Item -LiteralPath $sentinel }
    Remove-Item -LiteralPath $sentinelDir
    if ($createdDataRoot -and @(Get-ChildItem -LiteralPath $dataRoot -Force).Count -eq 0) {
        Remove-Item -LiteralPath $dataRoot
    }
}
if (@(Get-ChildItem -LiteralPath $env:TEMP -Filter '_MEI*' -Directory).Count) { throw 'Extraction directory left behind' }
if (Get-Process -Name CoveArchive,"CoveArchive-$version-Portable" -ErrorAction SilentlyContinue) { throw 'Worker left behind' }
Copy-Item -LiteralPath $portable -Destination $artifacts
$checksums = foreach ($name in @("CoveArchive-$version-Setup.exe", "CoveArchive-$version-Portable.exe")) {
    "$((Get-FileHash -Algorithm SHA256 -LiteralPath "$artifacts/$name").Hash.ToLower())  $name"
}
$checksums | Set-Content -Encoding utf8NoBOM "$artifacts/windows.sha256"
'sha256<<CHECKSUMS' | Add-Content -Encoding utf8NoBOM $env:GITHUB_OUTPUT
$checksums | Add-Content -Encoding utf8NoBOM $env:GITHUB_OUTPUT
'CHECKSUMS' | Add-Content -Encoding utf8NoBOM $env:GITHUB_OUTPUT
