function Get-InnoEngineVersion([string]$Output) {
    $triple = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    $versions = [regex]::Matches($Output, "(?m)^Compiler engine version: Inno Setup ($triple)`r?$")
    if ($versions.Count -ne 1) {
        throw 'Expected exactly one canonical Inno compiler engine version line'
    }
    return $versions[0].Groups[1].Value
}

function Assert-InnoVersion([string]$Compiler, [string]$PinnedVersion, [string]$ScratchDirectory) {
    $exitCode = 'not started'
    $stdout = ''; $stderr = ''
    $stdoutPath = $null; $stderrPath = $null
    try {
        $triple = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
        if (![regex]::IsMatch($PinnedVersion, "\A$triple\z")) {
            throw 'Invalid pinned Inno version (expected canonical major.minor.patch)'
        }
        $probeDir = Join-Path $ScratchDirectory ('inno-probe-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $probeDir -ErrorAction Stop | Out-Null
        $probe = Join-Path $probeDir 'probe.iss'
        # Output=no checks the script without producing setup files. All compiler
        # scratch output (including the default Output directory) stays here.
        @'
[Setup]
AppName=CoveArchive Inno probe
AppVersion=0.0.0
DefaultDirName={autopf}\CoveArchiveInnoProbe
Output=no
'@ | Set-Content -LiteralPath $probe -Encoding ASCII -ErrorAction Stop
        $stdoutPath = Join-Path $probeDir 'stdout.txt'
        $stderrPath = Join-Path $probeDir 'stderr.txt'
        $process = Start-Process -FilePath $Compiler -ArgumentList "`"$probe`"" -Wait -PassThru -NoNewWindow -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -ErrorAction Stop
        $exitCode = $process.ExitCode
        $stdout = [IO.File]::ReadAllText($stdoutPath)
        $stderr = [IO.File]::ReadAllText($stderrPath)
        if ($exitCode -ne 0) { throw 'Inno compiler probe failed' }
        $actualParts = (Get-InnoEngineVersion $stdout).Split('.')
        $pinnedParts = $PinnedVersion.Split('.')
        for ($i = 0; $i -lt 3; $i++) {
            if ([bigint]$actualParts[$i] -ne [bigint]$pinnedParts[$i]) {
                throw 'Inno version mismatch'
            }
        }
    } catch {
        $reason = $_.Exception.Message
        # Include any partial output even when starting or reading the probe fails.
        if ($stdoutPath -and (Test-Path -LiteralPath $stdoutPath)) {
            try { $stdout = [IO.File]::ReadAllText($stdoutPath) } catch {}
        }
        if ($stderrPath -and (Test-Path -LiteralPath $stderrPath)) {
            try { $stderr = [IO.File]::ReadAllText($stderrPath) } catch {}
        }
        $stdout = $stdout.Substring(0, [Math]::Min(2000, $stdout.Length))
        $stderr = $stderr.Substring(0, [Math]::Min(2000, $stderr.Length))
        throw "$reason; pinned=<$PinnedVersion>; exit code=<$exitCode>; stdout=<$stdout>; stderr=<$stderr>"
    }
}
