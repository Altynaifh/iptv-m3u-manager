from datetime import datetime
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship

class Subscription(SQLModel, table=True):
    """订阅源"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str # 订阅名称
    url: str # 链接（多个地址用逗号隔开）
    user_agent: str = Field(default="Mozilla/5.0") # 请求 UA
    headers: str = Field(default="{}")  # 额外请求头 (JSON)
    last_updated: datetime = Field(default_factory=datetime.utcnow) # 最后更新时间
    last_update_status: Optional[str] = None  # 最后更新状态（成功或错误信息）
    auto_update_minutes: int = Field(default=0) # 自动同步频率 (分钟)
    is_enabled: bool = Field(default=True) # 是否启用
    epg_url: Optional[str] = Field(default=None) # 自带 EPG 链接

    channels: List["Channel"] = Relationship(back_populates="subscription")

class Channel(SQLModel, table=True):
    """频道信息"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str # 频道名称
    url: str # 频道链接
    group: Optional[str] = None # 频道分组
    logo: Optional[str] = None # 台标链接
    tvg_id: Optional[str] = Field(default=None) # EPG ID
    subscription_id: int = Field(foreign_key="subscription.id") # 所属订阅
    is_enabled: bool = Field(default=True) # 是否启用该频道
    
    # 深度检测结果
    check_status: Optional[bool] = Field(default=None) # 检测是否通顺
    check_date: Optional[datetime] = Field(default=None) # 最后检测时间
    check_image: Optional[str] = Field(default=None) # 频道截图 (Base64)
    check_error: Optional[str] = Field(default=None) # 深度检测失败原因 (如无画面)
    check_source: Optional[str] = Field(default=None) # 检测来源: manual / auto

    # 视觉 AI 判定（拼图批处理）
    ai_visual_status: Optional[str] = Field(default=None)
    ai_visual_detail: Optional[str] = Field(default=None)
    ai_visual_date: Optional[datetime] = Field(default=None)
    
    subscription: Subscription = Relationship(back_populates="channels")

class OutputSource(SQLModel, table=True):
    """聚合源"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str # 输出源名称
    slug: str = Field(unique=True, index=True) # URL 路径标识符
    epg_url: Optional[str] = Field(default=None) # 聚合 EPG 链接
    include_source_suffix: bool = Field(default=True) # 频道名显示来源名
    filter_regex: str = Field(default=".*") # 正则过滤规则
    keywords: str = Field(default="[]") # 筛选关键字 (JSON)
    subscription_ids: str = Field(default="[]") # 关联订阅 ID (JSON)
    excluded_channel_ids: str = Field(default="[]") # 排除的频道 ID (JSON) - 聚合表级别排除
    last_updated: datetime = Field(default_factory=datetime.utcnow) # 最后同步时间
    last_update_status: Optional[str] = None # 最后同步状态
    last_request_time: Optional[datetime] = None # 最近被请求的时间
    is_enabled: bool = Field(default=True) # 是否启用该聚合源
    auto_update_minutes: int = Field(default=0) # 自动同步频率 (分钟)
    auto_visual_check: bool = Field(default=False) # 更新后自动截图检测 (FFmpeg)
    auto_disable_on_check: bool = Field(default=True) # 截图检测后根据结果自动启用/禁用频道
    auto_ai_vision_check: bool = Field(default=False)
    auto_ai_organize: bool = Field(default=False)
    enable_ai_vision: bool = Field(default=False) # UI 展开视觉 LLM 配置
    enable_ai_organize: bool = Field(default=False)
    ai_organize_prompt: str = Field(default="")  # 用户自定义 AI 排序/分组前置提示词
    layout_mode: str = Field(default="rules")  # rules | explicit
    channel_layout: str = Field(default='{"groups":[]}')
    layout_meta: str = Field(default="{}")

class AppSettings(SQLModel, table=True):
    """全站 LLM 配置（单行 id=1）"""
    id: int = Field(default=1, primary_key=True)
    llm_text_json: str = Field(default='{"base_url":"","api_key":"","model":""}')
    llm_vision_json: str = Field(default='{"base_url":"","api_key":"","model":""}')
    access_password_enabled: bool = Field(default=False)
    access_password_hash: str = Field(default="")

class TaskRecord(SQLModel, table=True):
    """全局任务记录"""
    id: Optional[str] = Field(default=None, primary_key=True) # Taskiq 的 task_id
    name: str # 任务显示名称
    status: str = Field(default="pending") # pending, running, success, failure, canceled
    progress: int = Field(default=0) # 进度百分比 0-100
    message: Optional[str] = None # 当前步骤描述或错误信息
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    result: Optional[str] = None # 任务执行结果 (JSON)
    is_shown: bool = Field(default=True) # 是否在 UI 任务中心显示
