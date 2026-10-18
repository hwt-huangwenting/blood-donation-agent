"""可观测层落库：一次完整对话（AgentRun）及其 LLM / 工具明细。

铁律：日志写库失败绝不影响主流程——全部 try/except 包住，只打警告。
"""
import logging
from typing import Any

from app.core.db import AsyncSessionLocal
from app.models.run import AgentRun, LlmCall, ToolCall

logger = logging.getLogger(__name__)


async def log_run(
    *,
    session_id: str,
    user_id: str,
    question: str,
    answer: str,
    status: str,
    total_tokens: int,
    total_cost: float,
    duration_ms: int,
    llm_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> int | None:
    """一次对话结束后整体落库（run + 明细一个事务）。失败返回 None。"""
    try:
        async with AsyncSessionLocal() as session:
            run = AgentRun(
                session_id=session_id,
                user_id=user_id,
                question=question,
                answer=answer,
                status=status,
                total_tokens=total_tokens,
                total_cost=total_cost,
                duration_ms=duration_ms,
            )
            session.add(run)
            await session.flush()  # 先拿到 run.id 串联明细

            for item in llm_calls:
                session.add(LlmCall(run_id=run.id, **item))
            for item in tool_calls:
                session.add(ToolCall(run_id=run.id, **item))

            await session.commit()
            return run.id
    except Exception as exc:  # noqa: BLE001 - 日志失败只告警，绝不上抛
        logger.warning("AgentRun 落库失败（不影响主流程）：%s", exc)
        return None
