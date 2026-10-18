"""可观测层数据模型：一次完整对话（AgentRun）及其明细（LlmCall / ToolCall）。

只写 Agent 侧 PostgreSQL；写入失败绝不影响主流程（见 core/observe.py）。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentRun(Base):
    """一次完整对话的汇总记录。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 状态口径：ok（正常回答）/ refused（1 轮结束且未调任何工具，视为拒答）
    # / fallback（达到最大轮次兜底）/ error（异常）
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 成本（元），单价来自配置；免费模型为 0
    total_cost: Mapped[float] = mapped_column(nullable=False, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LlmCall(Base):
    """ReAct 每一轮的 LLM 调用明细。"""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    # 请求/响应摘要（截断存储，仅用于排查，不存全量消息）
    request_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolCall(Base):
    """一次工具执行明细。arguments 只含模型侧参数（注入字段天然不在其中）。"""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB, nullable=True)
    # ok / error
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 写类工具的幂等键（如 create_appointment），便于核对幂等行为
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
