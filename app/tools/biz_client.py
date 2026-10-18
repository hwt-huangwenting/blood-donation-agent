"""业务系统 HTTP 客户端：Python 侧访问现有 Express 后端的唯一出口。

安全与边界约定：
- 绝不直连 MySQL，只走 HTTP 接口；
- 用户身份通过 Authorization: Bearer <用户JWT> 传递，JWT 由服务端上下文注入，
  绝不来自模型参数；
- 服务间凭证 X-Agent-Token 从配置读取，防止外部伪造 Agent 直连内部接口。

错误约定：所有失败（网络 / 超时 / 非 200）统一抛 BizApiError，
由工具层转成结构化 dict 回灌给模型，便于 LLM 用「系统繁忙」等话术回应。
"""
import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 单次请求超时（秒）与失败重试次数（不含首次）
REQUEST_TIMEOUT = 5.0
MAX_RETRIES = 2


class BizApiError(Exception):
    """业务接口调用失败。type 用于工具层/前端区分错误类别。"""

    def __init__(self, error_type: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status_code = status_code


def _headers(user_token: str) -> dict[str, str]:
    """构造请求头：用户 JWT + 服务间凭证。

    token 做 strip 清洗：HTTP 头只允许 ASCII，上游若带入不可见字符
    （如 BOM / 换行）会导致 httpx 直接抛编码异常。
    """
    token = user_token.strip().lstrip("﻿")
    return {
        "Authorization": f"Bearer {token}",
        "X-Agent-Token": settings.agent_service_token,
    }


async def _request(
    method: str,
    path: str,
    *,
    user_token: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """发起到现有后端的请求，返回统一响应包装中的 data 部分。

    后端统一响应格式为 {code, message, data}；code 非 200 视为业务错误。
    网络错误 / 超时 / 5xx 自动重试，4xx 不重试（客户端错误重试无意义）。
    """
    url = f"{settings.biz_api_base}{path}"
    last_error: BizApiError | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.request(
                    method,
                    url,
                    headers=_headers(user_token),
                    params=params,
                    json=json_body,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 网络层失败：可重试
            last_error = BizApiError("network_error", f"业务系统连接失败：{exc}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            break

        body: dict[str, Any]
        try:
            body = response.json()
        except ValueError:
            body = {}

        code = body.get("code", response.status_code)
        message = body.get("message") or response.text[:200]

        if response.status_code < 400 and code == 200:
            return body.get("data")

        # 业务/HTTP 错误：4xx 不重试，5xx 重试
        last_error = BizApiError(
            "biz_error" if response.status_code < 500 else "server_error",
            f"业务接口返回错误（HTTP {response.status_code}）：{message}",
            status_code=response.status_code,
        )
        if response.status_code < 500 or attempt >= MAX_RETRIES:
            break
        await asyncio.sleep(0.5 * (attempt + 1))

    logger.warning("业务接口调用失败 %s %s：%s", method, path, last_error)
    raise last_error  # type: ignore[misc]


async def get(path: str, *, user_token: str, params: dict[str, Any] | None = None) -> Any:
    """GET 现有后端接口，返回 data。"""
    return await _request("GET", path, user_token=user_token, params=params)


async def post(path: str, *, user_token: str, json_body: dict[str, Any] | None = None) -> Any:
    """POST 现有后端接口，返回 data。"""
    return await _request("POST", path, user_token=user_token, json_body=json_body)


# ---------------------------------------------------------------------------
# Mock 模式：USE_MOCK_BIZ=true 时不发真实 HTTP，返回固定数据打通链路。
# 字段结构与真实接口保持一致，供联调与桩测试使用。
# ---------------------------------------------------------------------------

_MOCK_PROFILE = {
    "user": {
        "id": 1,
        "username": "mock_donor",
        "real_name": "张三",
        "gender": "男",
        "blood_type": "O",
        "phone": "13800000000",
        "email": "mock@example.com",
        "verify_status": "approved",
    },
    "stats": {"total_records": 2, "total_volume": 700, "on_chain_records": 1},
    "eligibility": {
        "identity_verified": True,
        "last_donation_date": "2025-06-01",
        "days_since_last_donation": 320,
        "can_donate_whole_blood": True,
        "can_donate_component_blood": True,
    },
}

_MOCK_RECORDS = {
    "total": 2,
    "list": [
        {
            "record_no": "BD20250601000001",
            "donation_date": "2025-06-01",
            "donation_type": "whole_blood",
            "volume_ml": 400,
            "blood_type": "O",
            "location": "校医院献血点",
            "status": "on_chain",
            "tx_hash": "0xmocktxhash0001",
            "block_hash": "0xmockblockhash0001",
            "certificate_no": "CERT20250601001",
            "token_id": 1,
        },
        {
            "record_no": "BD20241201000002",
            "donation_date": "2024-12-01",
            "donation_type": "component_blood",
            "volume_ml": 300,
            "blood_type": "O",
            "location": "市中心血站",
            "status": "confirmed",
            "tx_hash": None,
            "block_hash": None,
            "certificate_no": None,
            "token_id": None,
        },
    ],
}

_MOCK_CERTIFICATE = {
    "record_no": "BD20250601000001",
    "record_status": "on_chain",
    "donation_date": "2025-06-01",
    "certificate": {
        "certificate_no": "CERT20250601001",
        "token_id": 1,
        "tx_hash": "0xmocktxhash0001",
    },
    "verification": {
        "verified": True,
        "message": "链上数据与库内记录一致，验真通过",
        "detail": {"name_hash": "ok", "date_hash": "ok", "volume_hash": "ok"},
    },
}

_MOCK_EVENTS = {
    "total": 2,
    "list": [
        {
            "id": 101,
            "title": "秋季校园无偿献血活动",
            "location": "校体育馆一层",
            "start_date": "2026-11-20",
            "end_date": "2026-11-21",
            "start_time": "09:00",
            "end_time": "16:00",
            "status": "ongoing",
        },
        {
            "id": 102,
            "title": "成分血专场捐献活动",
            "location": "市中心血站二楼",
            "start_date": "2026-12-05",
            "end_date": "2026-12-05",
            "start_time": "08:30",
            "end_time": "12:00",
            "status": "upcoming",
        },
    ],
}


async def mock_get(path: str, *, params: dict[str, Any] | None = None) -> Any:
    """Mock 版 GET：按路径前缀返回固定数据。"""
    if path.startswith("/api/agent/profile"):
        return _MOCK_PROFILE
    if path.startswith("/api/agent/records"):
        return _MOCK_RECORDS
    if path.startswith("/api/agent/certificate"):
        record_no = (params or {}).get("recordNo", "")
        if record_no and record_no != _MOCK_CERTIFICATE["record_no"]:
            raise BizApiError("biz_error", f"业务接口返回错误（HTTP 404）：未找到献血编号 {record_no} 的本人记录", 404)
        return _MOCK_CERTIFICATE
    if path.startswith("/api/donations/events/"):
        event_id = int(path.rsplit("/", 1)[-1])
        for event in _MOCK_EVENTS["list"]:
            if event["id"] == event_id:
                return event
        raise BizApiError("biz_error", f"业务接口返回错误（HTTP 404）：活动 {event_id} 不存在", 404)
    if path.startswith("/api/donations/events"):
        return _MOCK_EVENTS
    raise BizApiError("biz_error", f"Mock 未覆盖的接口路径：{path}", 404)


async def mock_post(path: str, *, json_body: dict[str, Any] | None = None) -> Any:
    """Mock 版 POST：正式报名返回固定 registrationId（不写任何真实数据）。"""
    if path.startswith("/api/donations/registrations"):
        return {"registrationId": 900001}
    raise BizApiError("biz_error", f"Mock 未覆盖的接口路径：{path}", 404)
