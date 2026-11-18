function Get-InnoEngineVersion([string]$Output) {
    $triple = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    $versions = [regex]::Matches($Output, "(?m)^Compiler engine version: Inno Setup ($triple)`r?$")
    if ($versions.Count -ne 1) {
        throw 'Expected exactly one canonical Inno compiler engine version line'
    }
    return $versions[0].Groups[1].Value
}

function Assert-InnoVersion([string]$Compiler, [string]$PinnedVersion, [string]$ScratchDirectory,
                            [ValidateRange(1, 2147483647)][int]$TimeoutMilliseconds = 120000) {
    $exitCode = 'not started'
    $stdout = ''; $stderr = ''
    $process = $null; $stdoutTask = $null; $stderrTask = $null
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
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $Compiler
        $startInfo.Arguments = "`"$probe`""
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = [System.Diagnostics.Process]::Start($startInfo)
        # Drain both pipes before waiting so a full pipe cannot stall the compiler.
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (!$process.WaitForExit($TimeoutMilliseconds)) {
            $exitCode = 'timed out'
            try { $process.Kill($true) } catch {
                # Windows PowerShell 5.1's .NET Framework lacks Kill(entireProcessTree).
                try { & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F 2>$null | Out-Null } catch {}
                try { $process.Kill() } catch {}
            }
            $null = $process.WaitForExit(1000)
            throw "Inno compiler probe timed out after $TimeoutMilliseconds ms"
        }
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        $exitCode = $process.ExitCode
        if ($exitCode -isnot [int]) { throw 'Inno compiler probe returned a null or non-integer exit code' }
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
        # Bound draining after a failed/timed-out probe, including inherited pipes.
        if ($null -ne $stdoutTask) {
            try { if ($stdoutTask.Wait(1000)) { $stdout = $stdoutTask.Result } } catch {}
        }
        if ($null -ne $stderrTask) {
            try { if ($stderrTask.Wait(1000)) { $stderr = $stderrTask.Result } } catch {}
        }
        $stdout = $stdout.Substring(0, [Math]::Min(2000, $stdout.Length))
        $stderr = $stderr.Substring(0, [Math]::Min(2000, $stderr.Length))
        throw "$reason; pinned=<$PinnedVersion>; exit code=<$exitCode>; stdout=<$stdout>; stderr=<$stderr>"
    } finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}
