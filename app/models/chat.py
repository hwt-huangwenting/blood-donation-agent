"""会话与消息持久化模型：对话历史落 PG，服务重启不丢失。

与可观测三表（agent_runs 等）职责不同：
- 可观测表是审计视角，记录每次运行的完整轨迹与指标；
- 这里的表是产品视角，支撑「历史会话列表 + 刷新后继续上次对话」。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChatSession(Base):
    """一次对话会话（对应前端一个会话条目）。"""

    __tablename__ = "chat_sessions"

    # 会话 ID 由服务端生成（uuid），直接作为主键，与 SSE done 事件下发的一致
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 标题取首条用户消息截断，供会话列表展示
    title: Mapped[str] = mapped_column(String(100), nullable=False, default="新对话")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ChatMessage(Base):
    """会话中的一条消息（仅 user/assistant 文本，不含工具轨迹）。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_chat_messages_session_id_id", "session_id", "id"),
    )
