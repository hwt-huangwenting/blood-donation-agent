"""FastAPI 应用入口：健康检查 + 对话 + HITL 确认链路 + 可观测查询。"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.appointment import router as appointment_router
from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.config import settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title="献血服务智能层（Agent 服务）",
    version="0.1.0",
    description="已有区块链献血存证系统之上的自然语言服务层",
)

# 跨域：仅允许现有前端 dev 地址（对话页由现有 frontend/ 承载）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def warn_mock_mode() -> None:
    """MOCK 模式启动告警：防止 clone 后忘配 .env 时拿假数据演示而不自知。"""
    if settings.use_mock_biz:
        logger.warning(
            "!! 当前处于 MOCK 模式：业务数据为内置假数据（USE_MOCK_BIZ=true），"
            "查询/报名结果均非真实，切勿用于演示或答辩 !!"
        )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体校验失败统一返回 400（Prompt 5 约定口径），而非 FastAPI 默认的 422。"""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(loc) for loc in first.get("loc", []) if loc != "body")
    message = f"请求参数校验失败：{field} {first.get('msg', '')}".strip()
    return JSONResponse(
        status_code=400, content={"code": 400, "message": message, "data": None}
    )


app.include_router(health_router)
app.include_router(chat_router)
app.include_router(appointment_router)
app.include_router(runs_router)
