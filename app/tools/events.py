"""工具：list_events —— 查询可报名的献血活动。

数据来源：现有后端 GET /api/donations/events（复用业务接口，带用户 JWT）。
身份边界：用户身份由服务端注入 user_token，工具不接受用户标识参数。
"""
import logging
from typing import Any

from app.config import settings
from app.tools import biz_client

logger = logging.getLogger(__name__)

# 一次最多返回给模型的活动条数，避免结果过长
MAX_EVENTS = 10
# 可报名活动的 status 口径（与 donation_events 枚举一致）：未开始 + 进行中
SIGNUP_OPEN_STATUSES = {"upcoming", "ongoing"}


async def list_events(
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    user_token: str,
) -> dict[str, Any]:
    """查询可报名的献血活动列表。

    Args:
        from_date: 起始日期（YYYY-MM-DD，含当天），按活动开始日期过滤。
        to_date: 结束日期（YYYY-MM-DD，含当天），按活动开始日期过滤。
        user_token: 服务端注入的用户 JWT（模型不可见）。

    Returns:
        成功时 {"ok": True, "events": [...]}；失败时 {"ok": False, "error": {...}}。
    """
    try:
        if settings.use_mock_biz:
            data = await biz_client.mock_get("/api/donations/events")
        else:
            # 现有接口仅支持单状态过滤，不满足「可报名」口径；
            # 拉全量后在本地按 status ∈ {upcoming, ongoing} + 日期过滤
            data = await biz_client.get(
                "/api/donations/events",
                user_token=user_token,
                params={"page": 1, "pageSize": 50},
            )
    except biz_client.BizApiError as exc:
        logger.warning("list_events 调用业务接口失败：%s", exc.message)
        return {
            "ok": False,
            "error": {
                "type": exc.error_type,
                "message": "业务系统暂时繁忙，请稍后再试" if exc.error_type != "biz_error" else exc.message,
            },
        }

    events: list[dict[str, Any]] = []
    for event in (data or {}).get("list", []):
        # 只保留可报名（未开始 / 进行中）的活动
        if event.get("status") not in SIGNUP_OPEN_STATUSES:
            continue
        start_date = str(event.get("start_date") or "")[:10]
        # 本地日期过滤（边界含当天）
        if from_date and start_date < from_date:
            continue
        if to_date and start_date > to_date:
            continue
        events.append(
            {
                "id": event.get("id"),
                "title": event.get("title"),
                "location": event.get("location"),
                "start_date": start_date,
                "end_date": str(event.get("end_date") or "")[:10],
                "start_time": event.get("start_time"),
                "end_time": event.get("end_time"),
                "status": event.get("status"),
            }
        )

    return {"ok": True, "total": len(events), "events": events[:MAX_EVENTS]}
