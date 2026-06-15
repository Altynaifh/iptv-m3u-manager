# IPTV M3U Manager — Agent 上下文

FastAPI + SQLModel/SQLite + Taskiq 的 IPTV 订阅与聚合管理工具。

## 项目碎片知识

<!-- cs-note managed: 用 cs-note 维护，新条目按下面分节追加 -->

### 编译与构建

- 依赖装在项目 `venv/`；本地 Python 命令用 `.\venv\Scripts\python.exe`，勿假定全局 `python` 可用。

### 运行与本地起服务

- 入口 `main.py`；启动后根路径返回 `static/index.html`，静态资源挂载在 `/static/`。
- 数据库表与字段迁移在 `main.py` 的 `migrate_db()`，新增模型字段须同步写迁移逻辑。

### 测试

- 快速验证导入或脚本： `.\venv\Scripts\python.exe -c "..."` 或 `.\venv\Scripts\python.exe main.py`。

### 命令与脚本陷阱

- Git commit 格式：`type:变更描述`（type 如 feat / fix / perf）；push 等 Git 操作须串行逐步执行。
- 改 Web UI 时以 `static/index.html` 与 `static/index.css` 为准；根目录 `index.html` 为副本，非服务入口。

### 路径与目录约定

- 聚合静态产物：`services/output_artifacts.py`；磁盘路径 `/data/artifacts/`（本地 `./data/artifacts/`），含 `exports/{slug}.m3u` 与 `previews/{id}.json.gz`；`GET /m3u/{slug}` 与 `GET /outputs/{id}/export-preview` 默认直读文件，`?force=1` / `?epg_refresh=1` 强制重建。
- 访问控制：`services/access_auth.py` + 中间件；`/`、`/static/`、`/m3u/`、`/api/auth/login`、`/api/auth/status` 为公开路径，其余 API 在开启密码后需登录。
- 订阅列表多选： `#sub_list` + `#sub_bulk_float`；选框复用 `preview-group-section__select` 样式类。

### 环境变量与凭证

- 管理页密码存在 `AppSettings`（`access_password_enabled` / `access_password_hash`），M3U 导出链接保持公开。

### 其他

- 默认输出中文；新增或修改的代码注释须中文。
- 未要求时不主动新建 markdown 文档、不顺手大范围重构；diff 只服务当前任务。