# 本机 FFmpeg 4.4（截图检测）

目录已包含（或应包含）：

`C:\Users\xianyu\Downloads\iptv-m3u-manager\tools\ffmpeg-4.4-win64\bin\ffmpeg.exe`

版本：**4.4.1-essentials**（对齐 Docker 内 **4.4.2**）

## 你不用配 PATH

程序默认优先使用该路径，**不要**再用系统里的 FFmpeg 7.x 做截图。

## 推荐启动方式

在项目根目录：

```powershell
powershell -ExecutionPolicy Bypass -File start-local.ps1
```

会自动设置 `FFMPEG_PATH` 并启动 `uvicorn`。

## 自己配置（可选）

1. 复制 `config\ffmpeg.env.example` → `config\ffmpeg.env`，改 `FFMPEG_PATH` 指向别的 `ffmpeg.exe`。
2. 或在启动前手动：

```powershell
$env:FFMPEG_PATH = "C:\Users\xianyu\Downloads\iptv-m3u-manager\tools\ffmpeg-4.4-win64\bin\ffmpeg.exe"
$env:ALLOW_SYSTEM_FFMPEG = "0"
uvicorn main:app --reload
```

## 若目录被删、需重新下载

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-ffmpeg-4.4.ps1
```

或：

```powershell
curl.exe -L -o "$env:TEMP\ffmpeg-4.4.1.zip" "https://github.com/GyanD/codexffmpeg/releases/download/4.4.1/ffmpeg-4.4.1-essentials_build.zip"
```

解压后把内含 `bin\ffmpeg.exe` 的文件夹放到 `tools\ffmpeg-4.4-win64\`。

## 验证

```powershell
cd C:\Users\xianyu\Downloads\iptv-m3u-manager
python -c "from services.stream_checker import StreamChecker; StreamChecker._ffmpeg_path=None; print(StreamChecker.get_ffmpeg_path())"
```

应打印 `...\tools\ffmpeg-4.4-win64\bin\ffmpeg.exe`。
