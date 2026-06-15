from fastapi import FastAPI, Response, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
import asyncio
import os
import json
from datetime import datetime, timedelta

from database import engine, create_engine, sqlite_url
from models import SQLModel, Subscription, Channel, OutputSource, TaskRecord, AppSettings
from routers import subscriptions, outputs, tools, channels, tasks, settings, auth
from services.access_auth import is_protection_enabled, is_request_authenticated
from task_broker import broker, update_task_status
import services.llm_tasks  # noqa: F401
import services.output_postprocess  # noqa: F401
import services.output_refresh  # noqa: F401
import services.output_artifacts  # noqa: F401
import uuid

app = FastAPI(title="IPTV M3U Manager")

_PUBLIC_PATHS = {"/"}
_PUBLIC_PREFIXES = ("/static/", "/m3u/", "/api/auth/login", "/api/auth/status")


@app.middleware("http")
async def access_password_guard(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
        return await call_next(request)
    with Session(engine) as session:
        if not is_protection_enabled(session):
            return await call_next(request)
    if is_request_authenticated(request):
        return await call_next(request)
    return JSONResponse(status_code=401, content={"detail": "未授权，请先登录"})

# 静态文件路径
if not os.path.exists("./static"):
    os.makedirs("./static", exist_ok=True)
app.mount("/static", StaticFiles(directory="./static"), name="static")

# 加载功能路由
app.include_router(subscriptions.router)
app.include_router(outputs.router)
app.include_router(tools.router)
app.include_router(channels.router)
app.include_router(tasks.router)
app.include_router(settings.router)
app.include_router(auth.router)

from sqlalchemy import text

def create_db_and_tables():
    """初始化数据库"""
    SQLModel.metadata.create_all(engine)

def migrate_db():
    """数据库迁移（加新字段）"""
    with Session(engine) as session:
        # 订阅表结构迁移
        try:
            session.exec(text("SELECT last_update_status FROM subscription LIMIT 1"))
        except:
            print("正在迁移 Subscription 表: 添加 last_update_status 字段")
            session.exec(text("ALTER TABLE subscription ADD COLUMN last_update_status VARCHAR"))
            session.commit()
            
        try:
            session.exec(text("SELECT auto_update_minutes FROM subscription LIMIT 1"))
        except:
            print("正在迁移 Subscription 表: 添加 auto_update_minutes 字段")
            session.exec(text("ALTER TABLE subscription ADD COLUMN auto_update_minutes INTEGER DEFAULT 0"))
            session.commit()

        try:
            session.exec(text("SELECT is_enabled FROM subscription LIMIT 1"))
        except:
            print("正在迁移 Subscription 表: 添加 is_enabled 字段")
            session.exec(text("ALTER TABLE subscription ADD COLUMN is_enabled BOOLEAN DEFAULT 1"))
            session.commit()

        try:
            session.exec(text("SELECT epg_url FROM subscription LIMIT 1"))
        except:
            print("正在迁移 Subscription 表: 添加 epg_url 字段")
            session.exec(text("ALTER TABLE subscription ADD COLUMN epg_url VARCHAR"))
            session.commit()
        
        # 频道表结构迁移
        try:
            session.exec(text("SELECT tvg_id FROM channel LIMIT 1"))
        except:
            print("正在迁移 Channel 表: 添加 tvg_id 字段")
            session.exec(text("ALTER TABLE channel ADD COLUMN tvg_id VARCHAR"))
            session.commit()

        # 聚合输出表结构迁移
        try:
            session.exec(text("SELECT epg_url FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 epg_url 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN epg_url VARCHAR"))
            session.commit()

        try:
            session.exec(text("SELECT include_source_suffix FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 include_source_suffix 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN include_source_suffix BOOLEAN DEFAULT 1"))
            session.commit()

        try:
            session.exec(text("SELECT last_updated FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 last_updated 和 last_update_status 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN last_updated DATETIME"))
            session.exec(text("ALTER TABLE outputsource ADD COLUMN last_update_status VARCHAR"))
            session.commit()
        
        try:
            session.exec(text("SELECT last_request_time FROM outputsource LIMIT 1"))
        except:
             print("正在迁移 OutputSource 表: 添加 last_request_time 字段")
             session.exec(text("ALTER TABLE outputsource ADD COLUMN last_request_time DATETIME"))
             session.commit()

        try:
            session.exec(text("SELECT is_enabled FROM channel LIMIT 1"))
        except:
            print("正在迁移 Channel 表: 添加 is_enabled 字段")
            session.exec(text("ALTER TABLE channel ADD COLUMN is_enabled BOOLEAN DEFAULT 1"))
            session.commit()
            
        try:
            session.exec(text("SELECT check_status FROM channel LIMIT 1"))
        except:
            print("正在迁移 Channel 表: 添加深度检测相关字段 (check_status, check_date, check_image)")
            session.exec(text("ALTER TABLE channel ADD COLUMN check_status BOOLEAN"))
            session.exec(text("ALTER TABLE channel ADD COLUMN check_date DATETIME"))
            session.exec(text("ALTER TABLE channel ADD COLUMN check_image VARCHAR"))
            session.commit()

        try:
            session.exec(text("SELECT check_error FROM channel LIMIT 1"))
        except:
            print("正在迁移 Channel 表: 添加 check_error 字段")
            session.exec(text("ALTER TABLE channel ADD COLUMN check_error VARCHAR"))
            session.commit()

        try:
            session.exec(text("SELECT check_source FROM channel LIMIT 1"))
        except:
             print("正在迁移 Channel 表: 添加 check_source 字段")
             session.exec(text("ALTER TABLE channel ADD COLUMN check_source VARCHAR"))
             session.commit()

        try:
            session.exec(text("SELECT is_enabled FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 is_enabled 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN is_enabled BOOLEAN DEFAULT 1"))
            session.commit()

        try:
            session.exec(text("SELECT auto_update_minutes FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 auto_update_minutes 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN auto_update_minutes INTEGER DEFAULT 0"))
            session.commit()

        try:
            session.exec(text("SELECT auto_visual_check FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 auto_visual_check 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN auto_visual_check BOOLEAN DEFAULT 0"))
            session.commit()

        # 聚合表级别频道排除功能
        try:
            session.exec(text("SELECT excluded_channel_ids FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource 表: 添加 excluded_channel_ids 字段")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN excluded_channel_ids VARCHAR DEFAULT '[]'"))
            session.commit()

        try:
            session.exec(text("SELECT layout_mode FROM outputsource LIMIT 1"))
        except:
            print("正在迁移 OutputSource: layout_mode, channel_layout, layout_meta")
            session.exec(text("ALTER TABLE outputsource ADD COLUMN layout_mode VARCHAR DEFAULT 'rules'"))
            session.exec(text('ALTER TABLE outputsource ADD COLUMN channel_layout VARCHAR DEFAULT \'{"groups":[]}\''))
            session.exec(text("ALTER TABLE outputsource ADD COLUMN layout_meta VARCHAR DEFAULT '{}'"))
            session.commit()

        try:
            session.exec(text("SELECT ai_visual_status FROM channel LIMIT 1"))
        except:
            print("正在迁移 Channel: ai_visual_*")
            session.exec(text("ALTER TABLE channel ADD COLUMN ai_visual_status VARCHAR"))
            session.exec(text("ALTER TABLE channel ADD COLUMN ai_visual_detail VARCHAR"))
            session.exec(text("ALTER TABLE channel ADD COLUMN ai_visual_date DATETIME"))
            session.commit()


        try:
            session.exec(text("SELECT access_password_enabled FROM appsettings LIMIT 1"))
        except:
            print("正在迁移 AppSettings: access_password_*")
            session.exec(text("ALTER TABLE appsettings ADD COLUMN access_password_enabled BOOLEAN DEFAULT 0"))
            session.exec(text("ALTER TABLE appsettings ADD COLUMN access_password_hash VARCHAR DEFAULT ''"))
            session.commit()

        for col, sql in [
            ("preview_cache_key", "ALTER TABLE outputsource ADD COLUMN preview_cache_key VARCHAR"),
            ("preview_cache_json", "ALTER TABLE outputsource ADD COLUMN preview_cache_json VARCHAR"),
            ("preview_cache_at", "ALTER TABLE outputsource ADD COLUMN preview_cache_at DATETIME"),
            ("member_total", "ALTER TABLE outputsource ADD COLUMN member_total INTEGER"),
            ("member_enabled", "ALTER TABLE outputsource ADD COLUMN member_enabled INTEGER"),
            ("member_disabled", "ALTER TABLE outputsource ADD COLUMN member_disabled INTEGER"),
            ("auto_ai_vision_check", "ALTER TABLE outputsource ADD COLUMN auto_ai_vision_check BOOLEAN DEFAULT 0"),
           ("ai_organize_prompt", "ALTER TABLE outputsource ADD COLUMN ai_organize_prompt VARCHAR DEFAULT ''"),
           ("auto_ai_organize", "ALTER TABLE outputsource ADD COLUMN auto_ai_organize BOOLEAN DEFAULT 0"),
           ("enable_ai_vision", "ALTER TABLE outputsource ADD COLUMN enable_ai_vision BOOLEAN DEFAULT 0"),
           ("enable_ai_organize", "ALTER TABLE outputsource ADD COLUMN enable_ai_organize BOOLEAN DEFAULT 0"),
            ("auto_disable_on_check", "ALTER TABLE outputsource ADD COLUMN auto_disable_on_check BOOLEAN DEFAULT 1"),
        ]:
            try:
                session.exec(text(f"SELECT {col} FROM outputsource LIMIT 1"))
            except:
                print(f"正在迁移 OutputSource: {col}")
                session.exec(text(sql))
                session.commit()

async def auto_update_task():
    """后台自动同步订阅"""
    while True:
        try:
            with Session(engine) as session:
                # 1. 更新订阅
                subs = session.exec(select(Subscription).where(
                    Subscription.auto_update_minutes > 0,
                    Subscription.is_enabled == True
                )).all()
                for sub in subs:
                    now = datetime.utcnow()
                    last = sub.last_updated or datetime.min
                    elapsed_mins = (now - last).total_seconds() / 60
                    
                    if elapsed_mins >= sub.auto_update_minutes:
                        print(f"[自动更新] 正在刷新订阅 {sub.id} ({sub.name})。已耗时: {elapsed_mins:.1f}分钟")
                        
                        # 派发异步任务
                        from services.fetcher import fetch_subscription_task
                        task_id = f"auto-sub-{sub.id}-{int(now.timestamp())}"
                        task_record = TaskRecord(
                            id=task_id,
                            name=f"自动同步订阅: {sub.name}",
                            status="pending",
                            is_shown=False # 自动任务默认不在前端弹窗，但在任务列表可见
                        )
                        session.add(task_record)
                        session.commit()
                        
                        await fetch_subscription_task.kiq(
                            task_id=task_id,
                            sub_id=sub.id,
                            url_str=sub.url,
                            ua=sub.user_agent,
                            headers_json=sub.headers
                        )
            
                # 2. 更新聚合源
                outputs = session.exec(select(OutputSource).where(
                    OutputSource.auto_update_minutes > 0,
                    OutputSource.is_enabled == True
                )).all()
                for out in outputs:
                        now = datetime.utcnow()
                        last = out.last_updated or datetime.min
                        elapsed_mins = (now - last).total_seconds() / 60

                        if elapsed_mins >= out.auto_update_minutes:
                            print(f"[自动更新] 正在刷新聚合源 {out.id} ({out.name})...")
                            try:
                                sub_ids = json.loads(out.subscription_ids)
                                # 此处不需要 process_subscription_refresh，因为步骤1已经刷过所有订阅
                                # 直接刷新聚合 EPG (如果有)
                                if out.epg_url:
                                    from services.epg import fetch_epg_cached
                                    await fetch_epg_cached(out.epg_url, refresh=True)
                                
                                out.last_updated = now
                                session.add(out)
                                session.commit()
                                print(f"[自动更新] 聚合源 {out.id} 同步完成。")

                                if out.auto_visual_check or out.auto_ai_vision_check or out.auto_ai_organize:
                                    from services.output_postprocess import output_postprocess_task
                                    task_id = f"auto-post-{out.id}-{int(now.timestamp())}"
                                    session.add(TaskRecord(id=task_id, name=f"自动后处理: {out.name}", status="pending", is_shown=False))
                                    session.commit()
                                    await output_postprocess_task.kiq(
                                        task_id=task_id,
                                        output_id=out.id,
                                        source='auto',
                                        force_check=False,
                                    )
                                else:
                                    from services.update_status_report import format_output_update_status
                                    out.last_update_status = format_output_update_status(
                                        "auto",
                                        sync=True,
                                        screenshot_skipped=True,
                                        ai_vision_skipped=True,
                                        ai_organize_skipped=True,
                                    )
                                    session.add(out)
                                    session.commit()
                            except Exception as e:
                                print(f"[自动更新] 聚合源 {out.id} 刷新失败: {e}")
                                from services.update_status_report import format_output_update_status
                                out.last_update_status = format_output_update_status("auto", sync=False, error=str(e))
                                session.add(out)
                                session.commit()
        except Exception as outer_e:
            print(f"[自动更新] 循环发生错误: {outer_e}")
            
        await asyncio.sleep(30) # 每隔 30 秒检查一次，提高 2 分钟测试任务的灵敏度

@app.on_event("startup")
async def on_startup():
    """启动时初始化"""
    create_db_and_tables()
    migrate_db()
    
    # 启动 Taskiq Broker
    await broker.startup()
    
    # 纠正“僵尸任务”：将重启前仍处于运行中或等待中的任务重置为已中止
    with Session(engine) as session:
        statement = select(TaskRecord).where(TaskRecord.status.in_(["running", "pending"]))
        zombie_tasks = session.exec(statement).all()
        if zombie_tasks:
            print(f"[System] 正在重置 {len(zombie_tasks)} 个僵尸任务记录...")
            for t in zombie_tasks:
                t.status = "canceled"
                t.message = "系统重启或非正常终止"
                session.add(t)
            session.commit()
    
    asyncio.create_task(auto_update_task())

    # 启动时补齐缺失的磁盘产物（后台生成，不阻塞首屏）
    import os
    from services.output_artifacts import m3u_artifact_path, preview_artifact_path, schedule_rebuild_output_artifacts

    with Session(engine) as session:
        for out in session.exec(select(OutputSource)).all():
            if out.id is None:
                continue
            if not os.path.isfile(m3u_artifact_path(out.slug)) or not os.path.isfile(preview_artifact_path(out.id)):
                schedule_rebuild_output_artifacts(out.id, epg_refresh=bool(out.epg_url))

@app.get("/")
def read_index():
    """返回主页文件"""
    with open("./static/index.html", encoding="utf-8") as f:
        return Response(content=f.read(), media_type="text/html")
