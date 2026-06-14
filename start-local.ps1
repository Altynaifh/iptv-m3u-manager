# 本地启动（使用项目内 FFmpeg 4.4 做截图检测）
# 用法: powershell -ExecutionPolicy Bypass -File start-local.ps1

$Root = $PSScriptRoot
$Ffmpeg = Join-Path $Root "tools\ffmpeg-4.4-win64\bin\ffmpeg.exe"

if (-not (Test-Path $Ffmpeg)) {
    Write-Host "缺少 FFmpeg 4.4，请先运行: scripts\install-ffmpeg-4.4.ps1"
    exit 1
}

$env:FFMPEG_PATH = $Ffmpeg
$env:ALLOW_SYSTEM_FFMPEG = "0"

Write-Host "FFMPEG_PATH=$env:FFMPEG_PATH"
& $Ffmpeg -version | Select-Object -First 1
Write-Host "启动 uvicorn ..."
Set-Location $Root

if (Test-Path (Join-Path $Root ".venv\Scripts\Activate.ps1")) {
    & (Join-Path $Root ".venv\Scripts\Activate.ps1")
} elseif (Test-Path (Join-Path $Root "venv\Scripts\Activate.ps1")) {
    & (Join-Path $Root "venv\Scripts\Activate.ps1")
}

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
