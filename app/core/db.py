"""Agent 侧 PostgreSQL 连接（只放向量与运行日志，不碰现有 MySQL 业务库）。"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# 单个进程内复用的异步引擎
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

# 会话工厂：expire_on_commit=False，避免提交后访问对象属性再触发查询
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：按请求提供会话。"""
    async with AsyncSessionLocal() as session:
        yield session
