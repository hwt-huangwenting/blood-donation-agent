"""HITL 确认链路：报名草稿的创建与确认提交。

铁律（与 Prompt 0 边界一致）：
- 健康征询五问与知情同意必须由用户本人作答，AI 不得代答——
  本接口对五项做严格布尔校验（StrictBool），缺失 / 非布尔 / 同意非 true 一律 400；
- 身份只来自 JWT（服务端注入），绝不接受 userId 参数；
- 幂等：已提交的草稿重复 confirm 直接返回上次结果，不产生重复报名。
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictBool

from app.api.deps import CurrentUser, get_current_user
from app.config import settings
from app.core.db import AsyncSessionLocal
from app.models.appointment import AppointmentDraft
from app.tools import biz_client
from app.tools.appointment import create_appointment

logger = logging.getLogger(__name__)
router = APIRouter()


class DraftRequest(BaseModel):
    """创建草稿请求体。健康征询相关字段**刻意不存在**，本阶段不接受。"""

    event_id: int
    donation_type: str


class ConfirmRequest(BaseModel):
    """确认提交请求体：用户本人在确认卡片上填写的内容。

    StrictBool：只接受 JSON true/false，字符串 / 数字 / 缺失都会被拒绝，
    从协议层杜绝「AI 代答」或脏数据混入健康征询。
    """

    donation_type: str
    has_cold_fever: StrictBool
    has_taken_medicine: StrictBool
    has_surgery: StrictBool
    has_chronic_disease: StrictBool
    has_adverse_reaction: StrictBool
    informed_consent: StrictBool


@router.post("/api/appointment/draft")
async def create_draft(
    req: DraftRequest, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    """生成报名草稿（与 AI 工具 create_appointment 同一实现，幂等）。"""
    result = await create_appointment(
        user_id=user.user_id,
        user_token=user.token,
        event_id=req.event_id,
        donation_type=req.donation_type,
    )
    if not result.get("ok"):
        error = result.get("error") or {}
        raise HTTPException(status_code=400, detail=error.get("message", "创建草稿失败"))
    return {"code": 200, "message": "报名草稿已生成，待您本人确认后提交", "data": result}


@router.post("/api/appointment/{draft_id}/confirm")
async def confirm_draft(
    draft_id: int,
    req: ConfirmRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """用户本人确认后，携带健康征询五问 + 知情同意提交正式报名。"""
    # 知情同意必须为 true，绝不默认代答
    if req.informed_consent is not True:
        raise HTTPException(status_code=400, detail="知情同意必须由您本人勾选后才能提交")

    async with AsyncSessionLocal() as session:
        draft = await session.get(AppointmentDraft, draft_id)
        # 不存在或不属于当前用户：统一 404，不暴露草稿存在性
        if draft is None or draft.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="报名草稿不存在")

        # 幂等：已提交过的草稿直接返回上次结果，不重复调后端报名
        if draft.status == "submitted":
            return {
                "code": 200,
                "message": "该草稿此前已提交，本次为幂等返回",
                "data": {
                    "draft_id": draft.id,
                    "registration_id": draft.registration_id,
                    "status": draft.status,
                    "idempotent_replay": True,
                },
            }

        if req.donation_type != draft.donation_type:
            raise HTTPException(
                status_code=400,
                detail=f"献血类型与草稿不一致（草稿为 {draft.donation_type}），请核对后重试",
            )

        # 携带用户 JWT 调现有后端正式报名（后端有同款严格校验，双保险；
        # 后端 authenticateToken 还会查库核对 token_version / 账号状态）
        payload = {
            "event_id": draft.event_id,
            "donation_type": draft.donation_type,
            "has_cold_fever": req.has_cold_fever,
            "has_taken_medicine": req.has_taken_medicine,
            "has_surgery": req.has_surgery,
            "has_chronic_disease": req.has_chronic_disease,
            "has_adverse_reaction": req.has_adverse_reaction,
            "informed_consent": True,
        }
        try:
            if settings.use_mock_biz:
                data = await biz_client.mock_post(
                    "/api/donations/registrations", json_body=payload
                )
            else:
                data = await biz_client.post(
                    "/api/donations/registrations",
                    user_token=user.token,
                    json_body=payload,
                )
        except biz_client.BizApiError as exc:
            # 后端拒绝（重复报名 / 间隔不足 / 活动不可报等）：
            # 草稿保持 draft 可修正重试，错误信息原样透传
            raise HTTPException(
                status_code=exc.status_code or 400, detail=exc.message
            ) from None

        registration_id = data.get("registrationId") if isinstance(data, dict) else None
        draft.status = "submitted"
        draft.registration_id = registration_id
        await session.commit()
        logger.info(
            "报名草稿已提交 draft_id=%s user=%s registration_id=%s",
            draft.id,
            user.user_id,
            registration_id,
        )

    return {
        "code": 200,
        "message": "报名已提交",
        "data": {
            "draft_id": draft.id,
            "registration_id": registration_id,
            "status": "submitted",
            "idempotent_replay": False,
        },
    }
