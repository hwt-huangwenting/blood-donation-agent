"""建表脚本：启用 pgvector 扩展并创建 Agent 侧所有表。

用法（在 agent-service/ 目录下）：
    .venv\\Scripts\\python.exe scripts/init_db.py
"""
import asyncio
import sys
from pathlib import Path

# 允许以 `python scripts/init_db.py` 方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.policy import PolicyChunk  # noqa: E402,F401 - 导入以注册模型元数据
from app.models.appointment import AppointmentDraft  # noqa: E402,F401 - 导入以注册模型元数据
from app.models.run import AgentRun, LlmCall, ToolCall  # noqa: E402,F401 - 导入以注册模型元数据
from app.models.chat import ChatMessage, ChatSession  # noqa: E402,F401 - 导入以注册模型元数据


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))).scalar()
        tables = sorted(Base.metadata.tables)

    print(f"pgvector 扩展版本：{version}")
    print(f"已建表：{', '.join(tables)}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
