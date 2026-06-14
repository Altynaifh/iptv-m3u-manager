# Install FFmpeg 4.4.x into tools/ffmpeg-4.4-win64 (screenshot check, align with Docker 4.4.2)
# Run from repo root:
#   powershell -ExecutionPolicy Bypass -File scripts/install-ffmpeg-4.4.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dest = Join-Path $Root "tools\ffmpeg-4.4-win64"
$Bin = Join-Path $Dest "bin\ffmpeg.exe"

if (Test-Path $Bin) {
    & $Bin -version | Select-Object -First 1
    Write-Host "OK already: $Bin"
    exit 0
}

$ZipUrl = "https://github.com/GyanD/codexffmpeg/releases/download/4.4.1/ffmpeg-4.4.1-essentials_build.zip"
$TempZip = Join-Path $env:TEMP "ffmpeg-4.4.1-essentials.zip"
$TempExtract = Join-Path $env:TEMP "ffmpeg-extract-$(Get-Random)"

Write-Host "Downloading FFmpeg 4.4.1 essentials..."
Invoke-WebRequest -Uri $ZipUrl -OutFile $TempZip -UseBasicParsing

New-Item -ItemType Directory -Force -Path $TempExtract | Out-Null
Expand-Archive -Path $TempZip -DestinationPath $TempExtract -Force

$inner = Get-ChildItem -Path $TempExtract -Directory | Select-Object -First 1
if (-not $inner) { throw "Empty extract dir" }

New-Item -ItemType Directory -Force -Path (Join-Path $Root "tools") | Out-Null
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
Copy-Item -Path $inner.FullName -Destination $Dest -Recurse

Remove-Item $TempZip -Force -ErrorAction SilentlyContinue
Remove-Item $TempExtract -Recurse -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $Bin)) {
    $alt = Get-ChildItem -Path $Dest -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if ($alt) {
        Write-Host "Found: $($alt.FullName)"
        Write-Host "Set FFMPEG_PATH to that file if needed."
    } else {
        throw "ffmpeg.exe not found under $Dest"
    }
} else {
    & $Bin -version | Select-Object -First 2
    Write-Host "Installed: $Bin"
}

Write-Host "Restart uvicorn; log should show: DEBUG: using bundled FFmpeg 4.4"

# Fallback download if gyan.dev fails:
# curl.exe -L -o "$env:TEMP\ffmpeg-4.4.1.zip" "https://github.com/GyanD/codexffmpeg/releases/download/4.4.1/ffmpeg-4.4.1-essentials_build.zip"
