"""健康检查路由。"""
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """服务存活探针。"""
    return {"status": "ok"}
