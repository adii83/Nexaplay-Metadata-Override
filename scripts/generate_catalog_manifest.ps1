[CmdletBinding()]
param(
    [string]$SourceRoot = ".",
    [string]$OutputPath = "catalog_manifest.json",
    [string]$Revision = "",
    [string]$BaseUrl = "https://raw.githubusercontent.com/adii83/Nexaplay-Metadata-Override/main"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$requiredFiles = @(
    "steam_data.json",
    "steam_data.json.gz",
    "override_data.json",
    "fix_games.json",
    "new_fix_games.json",
    "steam_games/steam_games.json",
    "appid_populer.json",
    "new_games.json"
)

$resolvedRoot = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not [System.IO.Directory]::Exists($resolvedRoot)) {
    throw "Source root not found: $resolvedRoot"
}

$baseUri = $null
if (-not [System.Uri]::TryCreate($BaseUrl, [System.UriKind]::Absolute, [ref]$baseUri) -or
    $baseUri.Scheme -ne [System.Uri]::UriSchemeHttps) {
    throw "BaseUrl must be an absolute HTTPS URL."
}

if ([string]::IsNullOrWhiteSpace($Revision)) {
    if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_SHA)) {
        $Revision = $env:GITHUB_SHA
    }
    else {
        $gitRevision = & git -C $resolvedRoot rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($gitRevision)) {
            $Revision = $gitRevision.Trim()
        }
        else {
            $Revision = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
        }
    }
}

$files = [ordered]@{}
foreach ($relativePath in $requiredFiles) {
    $nativeRelativePath = $relativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    $fullPath = [System.IO.Path]::Combine($resolvedRoot, $nativeRelativePath)
    if (-not [System.IO.File]::Exists($fullPath)) {
        throw "Required metadata file not found: $relativePath"
    }

    $info = [System.IO.FileInfo]::new($fullPath)
    if ($info.Length -le 0) {
        throw "Required metadata file is empty: $relativePath"
    }

    $files[$relativePath] = [ordered]@{
        url = "$($BaseUrl.TrimEnd('/'))/$relativePath"
        size = $info.Length
        sha256 = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

$manifest = [ordered]@{
    schema = 1
    revision = $Revision.Trim()
    files = $files
}

if ([string]::IsNullOrWhiteSpace($manifest.revision)) {
    throw "Revision cannot be empty."
}

$resolvedOutput = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    [System.IO.Path]::GetFullPath($OutputPath)
}
else {
    [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($resolvedRoot, $OutputPath))
}

$outputDirectory = [System.IO.Path]::GetDirectoryName($resolvedOutput)
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$tempPath = [System.IO.Path]::Combine(
    $outputDirectory,
    ".$([System.IO.Path]::GetFileName($resolvedOutput)).$([Guid]::NewGuid().ToString('N')).tmp")

try {
    $json = $manifest | ConvertTo-Json -Depth 5
    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($tempPath, "$json`n", $utf8WithoutBom)
    Move-Item -LiteralPath $tempPath -Destination $resolvedOutput -Force
}
finally {
    if ([System.IO.File]::Exists($tempPath)) {
        Remove-Item -LiteralPath $tempPath -Force
    }
}

Write-Host "Catalog manifest generated: $resolvedOutput"
Write-Host "Revision: $($manifest.revision)"
Write-Host "Files: $($requiredFiles.Count)"
