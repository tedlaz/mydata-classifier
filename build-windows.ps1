param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to build the application. Install it from https://docs.astral.sh/uv/"
}

$Version = (uv version --short).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Version)) {
    throw "Could not read a valid project version from pyproject.toml."
}
Write-Host "Building myDATA Classifier version $Version"

uv sync --dev
uv run pyinstaller --noconfirm --clean "mydata-classifier.spec"

if ($SkipInstaller) {
    Write-Host "Portable build created in: dist\myDATA Classifier"
    exit 0
}

$isccCommand = Get-Command iscc.exe -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { $null }
if (-not $isccPath) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $isccPath = $candidate
            break
        }
    }
}
if (-not $isccPath) {
    throw "Inno Setup 6 was not found. Install it or run with -SkipInstaller."
}

& $isccPath "/DMyAppVersion=$Version" "installer.iss"
Write-Host "Installer created in: installer-output"
