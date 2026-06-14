# 文本 LLM（AI 排序）配置说明

## 你当前的配置（已从数据库读出）

| 项 | 值 |
|----|-----|
| Base URL | `http://192.168.1.37:8317/v1` |
| 模型 | `grok-4.20-0309-non-reasoning` |
| API Key | 已保存（界面显示 `sk-3***51`） |
| 视觉 LLM | **未配置**（只影响 AI 视觉检测，不影响 AI 排序） |

聚合 **tvb** 已勾选 `enable_ai_organize: True`。

## 失败原因（已修复）

任务中心报错：`LLM HTTP 404`。

网关 `http://192.168.1.37:8317/v1` 的正确接口是：

- `http://192.168.1.37:8317/v1/chat/completions`

旧代码在 Base URL 已带 `/v1` 时又拼了一层 `/v1`，请求到了不存在的 `/v1/v1/chat/completions`。

**请重启 uvicorn** 后再试「AI 排序」。

## 使用 AI 排序前请确认

1. 聚合源已保存，且勾选 **「启用 AI 整理排序」**。
2. 预览里能看到 **AI 排序** 按钮（需 `enable_ai_organize=true`）。
3. 文本 LLM 三项都已填并 **保存 LLM 配置**。
4. 本机 `192.168.1.37:8317` 可从运行 uvicorn 的机器访问。

## Base URL 写法

两种都可以（程序会自动拼对路径）：

- `http://192.168.1.37:8317/v1`（推荐，与你现在一致）
- `http://192.168.1.37:8317`

不要多写路径，不要写成 `.../v1/chat/completions`（除非你自己改代码）。

## 自检命令

```powershell
cd C:\Users\xianyu\Downloads\iptv-m3u-manager
python -c "from sqlmodel import Session; from database import engine; from services.llm_settings import public_llm_settings; import json; print(json.dumps(public_llm_settings(Session(engine)), ensure_ascii=False, indent=2))"
```

## 视觉 LLM（可选）

要做 **AI 视觉检测**，还需在界面填 **视觉 LLM** 并勾选 **启用 AI 视觉检测**。与 AI 排序无关。

## 提示词版本

当前为 **standard-cn-groups-v1**（见 `services/playlist_organizer.py` 中 `ORGANIZE_SYSTEM`）：
- 忽略订阅 `source_group`
- 组间顺序：央视 → 卫视 → 港澳台 → 地方台 → 数字频道 → 其他
- 港台节目（含多种翡翠台写法）归入「港澳台」
