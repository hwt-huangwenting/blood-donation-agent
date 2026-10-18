"""政策知识库切片模型。"""
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.models.base import Base


class PolicyChunk(Base):
    """政策文档切片：一条记录 = 一个语义块 + 它的向量。"""

    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属文档标题（引用展示用）
    doc_title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 章节路径，如「二、献血间隔 / 全血献血」（引用展示用）
    section: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 原始文件相对路径，如 data/policy_docs/献血间隔规定.md
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    # 同一文档内的块序号，从 0 开始
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 切片原文
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 向量，维度取自配置（text-embedding-v3 默认 1024）
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dim), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<PolicyChunk {self.source_path}#{self.chunk_index}>"
