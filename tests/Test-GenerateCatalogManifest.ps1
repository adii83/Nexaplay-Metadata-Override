Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptPath = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($PSScriptRoot, "..", "scripts", "generate_catalog_manifest.ps1"))
$workflowPath = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($PSScriptRoot, "..", ".github", "workflows", "generate-catalog-manifest.yml"))
$testRoot = [System.IO.Path]::Combine(
    [System.IO.Path]::GetTempPath(),
    "nexaplay-catalog-manifest-$([Guid]::NewGuid().ToString('N'))")

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) {
        throw $Message
    }
}

try {
    $workflow = [System.IO.File]::ReadAllText($workflowPath)
    foreach ($required in @(
        "permissions:",
        "contents: write",
        "actions/checkout@v4",
        "tests/Test-GenerateCatalogManifest.ps1",
        "scripts/generate_catalog_manifest.ps1",
        "git pull --rebase origin main",
        "git push origin HEAD:main"
    )) {
        Assert-True ($workflow.Contains($required)) "Workflow is missing: $required"
    }
    foreach ($forbidden in @("push --force", "push -f", "reset --hard")) {
        Assert-True (-not $workflow.Contains($forbidden)) "Workflow contains forbidden command: $forbidden"
    }

    [System.IO.Directory]::CreateDirectory(
        [System.IO.Path]::Combine($testRoot, "steam_games")) | Out-Null

    foreach ($relativePath in @(
        "steam_data.json",
        "override_data.json",
        "fix_games.json",
        "new_fix_games.json",
        "steam_games/steam_games.json",
        "appid_populer.json"
    )) {
        $path = [System.IO.Path]::Combine(
            $testRoot,
            $relativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
        [System.IO.File]::WriteAllText($path, "{}")
    }
    [System.IO.File]::WriteAllText(
        [System.IO.Path]::Combine($testRoot, "new_games.json"),
        "[1]")

    $gzipPath = [System.IO.Path]::Combine($testRoot, "steam_data.json.gz")
    $file = [System.IO.File]::Create($gzipPath)
    try {
        $gzip = [System.IO.Compression.GZipStream]::new(
            $file,
            [System.IO.Compression.CompressionLevel]::Optimal)
        try {
            $writer = [System.IO.StreamWriter]::new($gzip)
            try { $writer.Write("{}") }
            finally { $writer.Dispose() }
        }
        finally { $gzip.Dispose() }
    }
    finally { $file.Dispose() }

    & $scriptPath -SourceRoot $testRoot -Revision "test-revision" -BaseUrl "https://example.test/main"
    $manifestPath = [System.IO.Path]::Combine($testRoot, "catalog_manifest.json")
    $firstBytes = [System.IO.File]::ReadAllBytes($manifestPath)
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json

    Assert-True ($manifest.schema -eq 1) "Manifest schema mismatch."
    Assert-True ($manifest.revision -eq "test-revision") "Manifest revision mismatch."
    Assert-True (($manifest.files.PSObject.Properties | Measure-Object).Count -eq 8) "Manifest must contain eight files."
    Assert-True ($manifest.files.'steam_games/steam_games.json'.url -eq "https://example.test/main/steam_games/steam_games.json") "Nested source URL mismatch."
    Assert-True ($manifest.files.'new_games.json'.size -eq 3) "File size mismatch."
    Assert-True ($manifest.files.'new_games.json'.sha256 -eq
        (Get-FileHash -LiteralPath ([System.IO.Path]::Combine($testRoot, "new_games.json")) -Algorithm SHA256).Hash.ToLowerInvariant()) "SHA-256 mismatch."

    & $scriptPath -SourceRoot $testRoot -Revision "test-revision" -BaseUrl "https://example.test/main"
    $secondBytes = [System.IO.File]::ReadAllBytes($manifestPath)
    Assert-True (
        [Convert]::ToBase64String($firstBytes) -eq [Convert]::ToBase64String($secondBytes)) `
        "Generator output must be deterministic."

    Remove-Item -LiteralPath ([System.IO.Path]::Combine($testRoot, "fix_games.json")) -Force
    $missingRejected = $false
    try {
        & $scriptPath -SourceRoot $testRoot -Revision "test-revision" -BaseUrl "https://example.test/main"
    }
    catch {
        $missingRejected = $true
    }
    Assert-True $missingRejected "Missing required metadata file was accepted."

    Write-Host "Catalog manifest generator self-check passed."
}
finally {
    if ([System.IO.Directory]::Exists($testRoot)) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
