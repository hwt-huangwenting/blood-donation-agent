"""工具：create_appointment —— 生成献血活动报名草稿。

边界约定（硬性）：
- 只写 Agent 侧 PostgreSQL 的草稿表，不直接调用后端正式报名接口；
  用户在前端确认卡片亲自填写健康征询并同意后，才走正式报名（Step 5）。
- 健康征询五问与知情同意不由本工具处理，更不接受相关参数。
- 幂等：同一「用户 × 活动 × 献血类型」重复请求只保留一份草稿。
"""
import hashlib
import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.core.db import AsyncSessionLocal
from app.models.appointment import AppointmentDraft
from app.tools import biz_client

logger = logging.getLogger(__name__)

# 允许的献血类型（与现有后端 donation_type 枚举一致）
ALLOWED_DONATION_TYPES = {"whole_blood", "component_blood"}


def _idempotency_key(user_id: str, event_id: int, donation_type: str) -> str:
    """幂等键：同一意向（用户 × 活动 × 类型）只对应一份草稿。"""
    raw = f"{user_id}:{event_id}:{donation_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_appointment(
    event_id: int,
    donation_type: str,
    *,
    user_id: str,
    user_token: str,
) -> dict[str, Any]:
    """生成报名草稿（不提交正式报名）。

    Args:
        event_id: 献血活动 id（须为 list_events 返回的真实活动 id）。
        donation_type: 献血类型，whole_blood（全血）或 component_blood（成分血）。
        user_id: 服务端注入的用户标识（模型不可见）。
        user_token: 服务端注入的用户 JWT（模型不可见）。

    Returns:
        成功时 {"ok": True, "draft": {...}, "idempotent_replay": bool}；
        失败时 {"ok": False, "error": {...}}。
    """
    # 参数白名单内的本地校验：类型不合法直接拒绝，不发请求
    if donation_type not in ALLOWED_DONATION_TYPES:
        return {
            "ok": False,
            "error": {
                "type": "invalid_param",
                "message": f"donation_type 只能是 whole_blood（全血）或 component_blood（成分血），收到：{donation_type}",
            },
        }

    # 校验活动真实存在（防止模型编造 event_id 生成无效草稿）
    try:
        if settings.use_mock_biz:
            event = await biz_client.mock_get(f"/api/donations/events/{event_id}")
        else:
            event = await biz_client.get(
                f"/api/donations/events/{event_id}", user_token=user_token
            )
    except biz_client.BizApiError as exc:
        logger.warning("create_appointment 校验活动失败 event_id=%s：%s", event_id, exc.message)
        return {
            "ok": False,
            "error": {
                "type": exc.error_type,
                "message": f"未找到活动 {event_id}，请先从 list_events 选择真实活动",
            },
        }

    key = _idempotency_key(user_id, event_id, donation_type)
    async with AsyncSessionLocal() as session:
        # 先查幂等键：已存在则直接返回既有草稿，不重复创建
        existing = (
            await session.execute(
                select(AppointmentDraft).where(AppointmentDraft.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # 状态对账：草稿已提交时，需核对业务侧报名是否仍然有效。
            # 用户可能在业务系统取消了报名——草稿不感知该动作，
            # 若直接幂等返回会导致确认卡片禁用、永远无法重新报名。
            if existing.status == "submitted" and not settings.use_mock_biz:
                reconciled = await _reconcile_submitted_draft(session, existing, user_token)
                if reconciled is not None:
                    return reconciled
            logger.info("报名草稿幂等命中 user=%s event=%s", user_id, event_id)
            return {
                "ok": True,
                "idempotent_replay": True,
                "draft": _draft_view(existing),
            }

        draft = AppointmentDraft(
            user_id=str(user_id),
            event_id=event_id,
            event_title=str(event.get("title") or ""),
            donation_type=donation_type,
            idempotency_key=key,
            status="draft",
        )
        session.add(draft)
        try:
            await session.commit()
        except Exception:
            # 并发下唯一约束冲突：回滚后重查，按幂等命中处理
            await session.rollback()
            existing = (
                await session.execute(
                    select(AppointmentDraft).where(AppointmentDraft.idempotency_key == key)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            return {"ok": True, "idempotent_replay": True, "draft": _draft_view(existing)}

    logger.info("报名草稿已创建 draft_id=%s user=%s event=%s", draft.id, user_id, event_id)
    return {"ok": True, "idempotent_replay": False, "draft": _draft_view(draft)}


async def _reconcile_submitted_draft(
    session, draft: AppointmentDraft, user_token: str
) -> dict[str, Any] | None:
    """已提交草稿的状态对账。

    查业务侧我的报名列表：若该活动的报名已被取消/驳回（或已不存在），
    把草稿重置回 draft 让用户可以重新确认提交；报名仍有效或核对失败时
    返回 None，走原幂等返回逻辑（保守，不影响正常重复提交拦截）。
    """
    try:
        registrations = await biz_client.get(
            "/api/donations/my-registrations", user_token=user_token
        )
    except biz_client.BizApiError as exc:
        logger.warning("报名状态对账失败 draft_id=%s：%s", draft.id, exc.message)
        return None

    if not isinstance(registrations, list):
        return None
    # 该活动下仍有效的报名（已登记/已审批）
    active = [
        r
        for r in registrations
        if isinstance(r, dict)
        and r.get("event_id") == draft.event_id
        and r.get("status") in ("registered", "approved")
    ]
    if active:
        return None

    # 报名已被取消/驳回或不存在：重置草稿，允许重新走确认提交流程
    draft.status = "draft"
    draft.registration_id = None
    await session.commit()
    logger.info(
        "报名已被业务侧取消，草稿已重置为待确认 draft_id=%s user=%s event=%s",
        draft.id,
        draft.user_id,
        draft.event_id,
    )
    return {
        "ok": True,
        "idempotent_replay": False,
        "reactivated": True,
        "draft": _draft_view(draft),
    }


def _draft_view(draft: AppointmentDraft) -> dict[str, Any]:
    """草稿的对外视图（供模型转述 / 前端展示）。"""
    return {
        "draft_id": draft.id,
        "event_id": draft.event_id,
        "event_title": draft.event_title,
        "donation_type": draft.donation_type,
        "status": draft.status,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }
