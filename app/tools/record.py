"""工具：query_record —— 查询本人献血记录 / 证书状态。

身份边界：用户身份由服务端注入 user_token（JWT），工具本身不接受任何用户标识参数。
失败时返回结构化错误 dict（而不是抛异常），便于 LLM 用「系统繁忙」等话术回应用户。
"""
import logging
from typing import Any

from app.config import settings
from app.tools import biz_client

logger = logging.getLogger(__name__)


def _error_result(exc: biz_client.BizApiError) -> dict[str, Any]:
    """把业务接口异常转成 LLM 可读的结构化错误。"""
    return {
        "ok": False,
        "error": {
            "type": exc.error_type,
            "message": "业务系统暂时繁忙，请稍后再试" if exc.error_type != "biz_error" else exc.message,
        },
    }


async def query_record(record_no: str | None = None, *, user_token: str) -> dict[str, Any]:
    """查询当前用户的献血记录与证书状态。

    Args:
        record_no: 献血编号（BD 开头）。不传时返回整体摘要 + 最近记录列表；
                   传入时返回该编号的证书状态与链上验真结果。
        user_token: 服务端注入的用户 JWT（模型不可见）。

    Returns:
        成功时 {"ok": True, ...}；失败时 {"ok": False, "error": {...}}。
    """
    try:
        if settings.use_mock_biz:
            if record_no:
                data = await biz_client.mock_get(
                    "/api/agent/certificate", params={"recordNo": record_no}
                )
                return {"ok": True, "mode": "single", **data}
            profile = await biz_client.mock_get("/api/agent/profile")
            records = await biz_client.mock_get("/api/agent/records")
        else:
            if record_no:
                data = await biz_client.get(
                    "/api/agent/certificate",
                    user_token=user_token,
                    params={"recordNo": record_no},
                )
                return {"ok": True, "mode": "single", **data}
            profile = await biz_client.get("/api/agent/profile", user_token=user_token)
            records = await biz_client.get("/api/agent/records", user_token=user_token)
    except biz_client.BizApiError as exc:
        logger.warning("query_record 调用业务接口失败：%s", exc.message)
        return _error_result(exc)

    return {
        "ok": True,
        "mode": "summary",
        "profile": profile,
        "records": records,
    }
