"""四宫格拼图 + 视觉 LLM 批处理。"""
import base64
import io
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont
from sqlmodel import Session, select

from models import Channel, OutputSource
from services.llm_client import LlmClient, VisionJsonParseError
from task_broker import TaskCanceledError, is_task_canceled
from services.llm_settings import load_llm_blocks
from services.stream_checker import StreamChecker

BATCH_SIZE = 4
VALID_STATUS = frozenset({"ok", "promo_loop", "invalid", "frozen", "no_image", "error"})
# 与导出门禁、前端「AI无效」角标一致：宣传垫片与无效画面均自动禁用
DISABLE_STATUSES = frozenset({"invalid", "promo_loop"})
ENABLE_STATUSES = frozenset({"ok", "frozen"})

SYSTEM_PROMPT = """你是 IPTV 频道画面质检员。根据 2x2 拼图判断每个槽位。原则：**从宽认定有效**，避免误杀正常收视。

状态含义（请严格遵守）：
- ok：能辨认出**电视节目内容**即可——剧集、电影、新闻、体育、综艺、带字幕/角标/台标、备用线路、BD/高清标识、单帧截图看起来像正在播的节目。**有实质画面内容一律优先 ok**。
- promo_loop：**仅当**画面主体是**可辨认的频道宣传/引流文案**（非正在播出的节目），例如：扫码观看、微信/APP 扫码、关注公众号、会员开通提示、频道自宣海报、纯文字/二维码引导页、**无影视剧/新闻/赛事画面的整屏宣传语**。
  **禁止**仅凭「像广告、像垫片、品牌 logo 循环、画面重复」判 promo_loop——单张截图**无法**判断是否循环垫片或品牌循环，这类一律用 **ok**。
  **禁止**把带字幕的电视剧、新闻、综艺、体育、台标+节目画面判为 promo_loop。
- invalid：**仅当**几乎全黑、彩条、报错页、雪花无内容、明显“无信号/连接失败”等**无法观看**；若槽位里能看见人物/场景/字幕/台标等，**不得**判 invalid（即使较暗或局部黑边）。
- frozen：画面长时间完全静止且像卡死（与正常暂停/片尾字幕区分）；不确定时用 ok。
- no_image：该槽位无图或无法辨认。

**拿不准时一律输出 ok**，detail 可写你看到的画面类型。
只输出 JSON：{"results":[{"channel_id":数字,"status":"...","detail":"简短中文"}]}
detail 勿含英文双引号 "，改用中文引号「」或省略；必须合法 JSON，禁止 markdown 代码块。"""

# 无自定义提示词时，每批 user 消息附带的默认判定口径
_DEFAULT_BATCH_RULES = (
    "请逐槽位输出 results。"
    "promo_loop 仅限整屏宣传语/扫码引流；无节目画面的广告垫片、品牌循环勿判 promo_loop。"
    "有剧集/新闻画面即 ok；仅全黑/无信号为 invalid。"
)


def _resolve_vision_prompt(
    out: Optional[OutputSource] = None,
    draft: Optional[dict] = None,
    custom_prompt: Optional[str] = None,
) -> str:
    """合并用户前置提示词：draft 优先，其次聚合源已存配置。"""
    prompt = (custom_prompt or "").strip()
    if not prompt and draft and isinstance(draft, dict):
        prompt = (draft.get("ai_vision_prompt") or "").strip()
    if not prompt and out is not None:
        prompt = (getattr(out, "ai_vision_prompt", "") or "").strip()
    return prompt


def _build_vision_system(custom_prompt: str) -> str:
    prompt = (custom_prompt or "").strip()
    if not prompt:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + "\n\n【用户自定义判定要求】\n"
        + prompt
        + "\n\n【合并规则】\n"
        + "1. 系统默认规则仅适用于用户未明确覆盖的判定场景。\n"
        + "2. 用户自定义要求与默认规则冲突时，以用户要求为准。\n"
        + "3. 用户未提及的状态含义与边界，仍按系统默认规则执行。\n"
        + "4. 输出仍为合法 JSON；status 仅限 ok / promo_loop / invalid / frozen / no_image / error。\n"
        + "5. 程序固定将 invalid、promo_loop 判为需禁用，ok、frozen 判为可启用；"
        + "你只负责判定 status，无法改变程序启禁策略。\n"
    )


def _build_vision_user_text(slot_count: int, slot_desc: str, custom_prompt: str) -> str:
    """拼装每批拼图请求的 user 文本；有自定义时写入 user 消息，避免被硬编码默认口径覆盖。"""
    header = (
        f"拼图共 {slot_count} 个槽位（左上→右上→左下→右下）：\n"
        f"{slot_desc}\n"
    )
    prompt = (custom_prompt or "").strip()
    if not prompt:
        return header + _DEFAULT_BATCH_RULES
    return (
        header
        + "请逐槽位输出 results。\n\n"
        + "【本批须遵守的用户补充判定】\n"
        + prompt
        + "\n\n【格式】只输出 JSON results；detail 用中文简短描述，勿用 markdown 代码块。"
    )


def _apply_ai_visual_enablement(ch: Channel, status: str) -> None:
    """按 AI 视觉结果更新启用状态（不删聚合成员）。"""
    st = (status or "error").lower()
    if st in DISABLE_STATUSES:
        ch.is_enabled = False
    elif st in ENABLE_STATUSES:
        ch.is_enabled = True


def _sync_stored_ai_visual_disablement(ch: Channel) -> bool:
    """按已落库的 AI 视觉状态补齐禁用（修复历史 promo_loop 未禁用）。"""
    st = (ch.ai_visual_status or "").lower()
    if st in DISABLE_STATUSES and ch.is_enabled:
        ch.is_enabled = False
        return True
    return False


def _resolve_result_channel_id(
    item: dict, batch: List[Channel], idx: Optional[int] = None
) -> Optional[int]:
    """匹配模型返回的 channel_id（兼容字符串、槽位与结果顺序回退）。"""
    raw = item.get("channel_id")
    if raw is not None and str(raw).strip() != "":
        try:
            cid = int(raw)
            if any(c.id == cid for c in batch):
                return cid
        except (TypeError, ValueError):
            pass
    slot = item.get("slot")
    try:
        si = int(slot) - 1
        if 0 <= si < len(batch) and batch[si].id is not None:
            return batch[si].id
    except (TypeError, ValueError):
        pass
    if idx is not None and 0 <= idx < len(batch) and batch[idx].id is not None:
        return batch[idx].id
    return None


def _decode_data_url(data_url: str) -> Optional[bytes]:
    """解析 data URL 或纯 base64 截图，供拼图使用。"""
    if not data_url:
        return None
    s = data_url.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if "," in s:
        s = s.split(",", 1)[1]
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


def build_collage_data_url(slots: List[Tuple[int, str, Optional[bytes]]]) -> str:
    """slots: (channel_id, label, jpeg_bytes or None)，长度 1-4，不足补空白。"""
    # 单格 480×270，整图 960×540，便于视觉模型辨认字幕/台标
    cell_w, cell_h = 480, 270
    img = Image.new("RGB", (cell_w * 2, cell_h * 2), (32, 32, 32))
    draw = ImageDraw.Draw(img)
    positions = [(0, 0), (cell_w, 0), (0, cell_h), (cell_w, cell_h)]
    for i in range(4):
        x, y = positions[i]
        if i < len(slots):
            cid, label, raw = slots[i]
            if raw:
                try:
                    tile = Image.open(io.BytesIO(raw)).convert("RGB")
                    tile.thumbnail((cell_w, cell_h))
                    img.paste(tile, (x + (cell_w - tile.width) // 2, y + (cell_h - tile.height) // 2))
                except Exception:
                    draw.rectangle([x, y, x + cell_w, y + cell_h], outline=(80, 80, 80))
            draw.text((x + 6, y + 6), f"#{cid} {label[:12]}", fill=(255, 220, 100))
        else:
            draw.rectangle([x, y, x + cell_w, y + cell_h], outline=(60, 60, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


async def ensure_check_image(session: Session, channel: Channel) -> Optional[str]:
    if channel.id is not None:
        row = session.get(Channel, channel.id)
        if row is not None:
            channel = row
    if channel.check_image:
        return channel.check_image
    res = await StreamChecker.check_stream_visual(channel.url)
    if res.get("status") and res.get("image"):
        channel.check_status = True
        channel.check_image = res["image"]
        channel.check_date = datetime.utcnow()
        channel.check_error = None
        session.add(channel)
        session.commit()
        session.refresh(channel)
        return channel.check_image
    channel.check_status = False
    channel.check_error = res.get("error") or "capture failed"
    session.add(channel)
    session.commit()
    return None


class VisualAiChecker:
    @classmethod
    async def run_batch(
        cls,
        session: Session,
        channels: List[Channel],
        *,
        capture_missing: bool = True,
        task_id: Optional[str] = None,
        progress_cb=None,
        out: Optional[OutputSource] = None,
        draft: Optional[dict] = None,
        custom_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        blocks = load_llm_blocks(session)
        vision = blocks["llm_vision"]
        client = LlmClient(vision["base_url"], vision["api_key"], vision["model"])
        if not client.configured():
            raise ValueError("视觉 LLM 未配置")

        unique = []
        seen = set()
        for ch in channels:
            if ch.id and ch.id not in seen:
                unique.append(ch)
                seen.add(ch.id)
        ids = [ch.id for ch in unique if ch.id is not None]
        if ids:
            by_id = {c.id: c for c in session.exec(select(Channel).where(Channel.id.in_(ids))).all()}
            unique = [by_id[i] for i in ids if i in by_id]

        total_batches = (len(unique) + BATCH_SIZE - 1) // BATCH_SIZE or 0
        done = 0
        stats = {"batches": 0, "updated": 0, "errors": 0, "disabled": 0, "enabled": 0}
        resolved_prompt = _resolve_vision_prompt(out, draft, custom_prompt)
        vision_system = _build_vision_system(resolved_prompt)

        async def _push_batch_realtime(batch_channels: List[Channel]) -> None:
            if not out or out.id is None:
                return
            from services.output_resolver import aggregate_channels
            from services.realtime_push import (
                broadcast_channel_patch,
                broadcast_preview_stats,
                channel_patch_fields,
            )

            patches = [channel_patch_fields(ch) for ch in batch_channels if ch.id is not None]
            if patches:
                await broadcast_channel_patch(out.id, patches)
            members = aggregate_channels(session, out, draft)
            enabled_n = sum(1 for c in members if c.is_enabled)
            await broadcast_preview_stats(out.id, len(members), enabled_n)

        for ch in unique:
            if _sync_stored_ai_visual_disablement(ch):
                stats["disabled"] += 1
                session.add(ch)
        if stats["disabled"]:
            session.commit()

        for batch_start in range(0, len(unique), BATCH_SIZE):
            if task_id and await is_task_canceled(task_id):
                raise TaskCanceledError("任务已中止")

            batch = unique[batch_start : batch_start + BATCH_SIZE]
            slots = []
            for ch in batch:
                img = ch.check_image
                if not img and capture_missing:
                    img = await ensure_check_image(session, ch)
                raw = _decode_data_url(img) if img else None
                slots.append((ch.id, ch.name or "", raw))

            if not any(s[2] for s in slots):
                for ch in batch:
                    if ch.check_image:
                        ch.ai_visual_status = "error"
                        ch.ai_visual_detail = "截图数据无法解析，未送入拼图"
                    else:
                        ch.ai_visual_status = "no_image"
                        ch.ai_visual_detail = "无截图"
                    ch.ai_visual_date = datetime.utcnow()
                    session.add(ch)
                session.commit()
                await _push_batch_realtime(batch)
                stats["updated"] += len(batch)
                done += 1
                if progress_cb:
                    await progress_cb(done, total_batches, "跳过无图批次")
                continue

            collage_url = build_collage_data_url(slots)
            slot_desc = "\n".join(
                f"slot{i+1}: channel_id={slots[i][0]} name={slots[i][1]}"
                for i in range(len(slots))
            )
            user_text = _build_vision_user_text(len(slots), slot_desc, resolved_prompt)

            try:
                parsed = await client.chat_vision_json(vision_system, user_text, collage_url)
                results = parsed.get("results") or []
            except (VisionJsonParseError, ValueError) as e:
                stats["errors"] += 1
                for ch in batch:
                    ch.ai_visual_status = "error"
                    ch.ai_visual_detail = str(e)[:200]
                    ch.ai_visual_date = datetime.utcnow()
                    session.add(ch)
                session.commit()
                await _push_batch_realtime(batch)
                done += 1
                if progress_cb:
                    await progress_cb(done, total_batches, f"批次失败: {e}")
                continue

            by_id = {ch.id: ch for ch in batch}
            seen_result = set()
            disabled_n = 0
            enabled_n = 0
            for idx, item in enumerate(results):
                cid = _resolve_result_channel_id(item, batch, idx)
                if cid is None:
                    continue
                ch = by_id.get(cid)
                if not ch or cid in seen_result:
                    continue
                seen_result.add(cid)
                st = (item.get("status") or "error").lower()
                if st not in VALID_STATUS:
                    st = "error"
                ch.ai_visual_status = st
                ch.ai_visual_detail = (item.get("detail") or "")[:500]
                ch.ai_visual_date = datetime.utcnow()
                prev = ch.is_enabled
                _apply_ai_visual_enablement(ch, st)
                if ch.is_enabled is False and prev is not False:
                    disabled_n += 1
                elif ch.is_enabled is True and prev is False:
                    enabled_n += 1
                session.add(ch)
                stats["updated"] += 1
            stats["disabled"] = stats.get("disabled", 0) + disabled_n
            stats["enabled"] = stats.get("enabled", 0) + enabled_n

            for ch in batch:
                if ch.id not in seen_result:
                    ch.ai_visual_status = "error"
                    ch.ai_visual_detail = "模型未返回该频道"
                    ch.ai_visual_date = datetime.utcnow()
                    session.add(ch)
                    stats["updated"] += 1

            session.commit()
            await _push_batch_realtime(batch)

            stats["batches"] += 1
            done += 1
            if progress_cb:
                await progress_cb(done, total_batches, f"已完成拼图批 {done}/{total_batches}")

        return stats
