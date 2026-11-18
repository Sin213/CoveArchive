function Assert-InnoVersion($VersionInfo, [string]$PinnedVersion) {
    $diagnostic = "ProductVersion=<$($VersionInfo.ProductVersion)>; FileVersion=<$($VersionInfo.FileVersion)>; pinned=<$PinnedVersion>"
    # Canonical decimal components reject leading-zero lookalikes and suffixes.
    $triple = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    if ($PinnedVersion -cnotmatch "\A$triple\z") {
        throw "Invalid pinned Inno version (expected major.minor.patch): $diagnostic"
    }
    $pinnedParts = $PinnedVersion.Split('.')
    $actual = ([string]$VersionInfo.ProductVersion).Trim()
    if ($actual -cnotmatch "\A$triple(\.(0|[1-9][0-9]*))?\z") {
        throw "Inno version mismatch (invalid ProductVersion): $diagnostic"
    }
    $actualParts = $actual.Split('.')
    $numericParts = @($VersionInfo.ProductMajorPart, $VersionInfo.ProductMinorPart, $VersionInfo.ProductBuildPart)
    for ($i = 0; $i -lt 3; $i++) {
        if ([bigint]$actualParts[$i] -ne [bigint]$pinnedParts[$i] -or
            $null -eq $numericParts[$i] -or [bigint]$numericParts[$i] -ne [bigint]$pinnedParts[$i]) {
            throw "Inno version mismatch: $diagnostic"
        }
    }
}
