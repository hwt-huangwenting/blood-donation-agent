"""FastAPI 依赖：校验前端带来的用户 JWT，解出当前用户身份。

注意（与后端 authenticateToken 的差异）：Agent 侧按硬约束**不直连 MySQL**，
因此只做签名与有效期校验，无法查库核对 token_version / 账号状态。
安全性由链路兜住：凡涉及写操作（confirm 正式报名）都会把原始 token 转发给
Express 后端，由后端 authenticateToken 做查库二次校验；只读接口（runs/stats）
接受此弱校验。
"""
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException

from app.config import settings


@dataclass(frozen=True)
class CurrentUser:
    """从 JWT 解出的当前登录用户。"""

    user_id: str
    username: str
    role: str
    # 原始 token，用于转发给后端业务接口（保持「本人身份」链路）
    token: str


async def get_current_user(authorization: str = Header(default="")) -> CurrentUser:
    """解析 Authorization: Bearer <jwt> 并验签。失败一律 401。"""
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未提供认证令牌，请先登录")

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            # 后端旧 token 可能无 tokenVersion，字段不做强制要求
            options={"require": ["id"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期，请重新登录") from None

    return CurrentUser(
        user_id=str(payload["id"]),
        username=str(payload.get("username", "")),
        role=str(payload.get("role", "")),
        token=token,
    )
