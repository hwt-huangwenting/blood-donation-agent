"""会话历史存储：对话消息持久化到 PostgreSQL（chat_sessions / chat_messages）。

替代原先的进程内字典记忆：服务重启、多实例部署后历史仍在，
支撑前端「历史会话列表 + 刷新后继续上次对话」。

安全约定：所有读取都必须带 user_id 过滤，用户只能看到自己的会话。
"""
from sqlalchemy import delete, func, select, update

from app.core.db import AsyncSessionLocal
from app.models.chat import ChatMessage, ChatSession

# 标题取首条用户消息的前 N 个字
TITLE_MAX_LEN = 30
# 单条消息入库上限，防止异常长文本撑爆行
MESSAGE_MAX_LEN = 8000


def _make_title(content: str) -> str:
    """用首条用户消息生成会话标题。"""
    title = content.strip().replace("\n", " ")[:TITLE_MAX_LEN]
    return title or "新对话"


async def get_or_create_session(session_id: str, user_id: str) -> None:
    """确保会话行存在（归属当前用户）。不存在则创建。"""
    async with AsyncSessionLocal() as session:
        row = await session.get(ChatSession, session_id)
        if row is None:
            session.add(ChatSession(id=session_id, user_id=user_id))
            await session.commit()


async def load_history(session_id: str, user_id: str) -> list[dict[str, str]]:
    """读取会话全部消息（时间升序），供 ReAct 上下文与前端回显。

    会话不属于当前用户时返回空列表（不暴露存在性）。
    """
    async with AsyncSessionLocal() as session:
        owner = await session.get(ChatSession, session_id)
        if owner is None or owner.user_id != user_id:
            return []
        result = await session.execute(
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
        )
        return [{"role": role, "content": content} for role, content in result.all()]


async def append_message(
    session_id: str, user_id: str, role: str, content: str
) -> None:
    """追加一条消息；首条用户消息同时设置会话标题并刷新更新时间。"""
    async with AsyncSessionLocal() as session:
        session.add(
            ChatMessage(
                session_id=session_id, role=role, content=content[:MESSAGE_MAX_LEN]
            )
        )
        # 首条用户消息定标题
        if role == "user":
            row = await session.get(ChatSession, session_id)
            if row is not None and row.title == "新对话":
                row.title = _make_title(content)
        # 显式刷新 updated_at，驱动会话列表按最近活跃排序
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(updated_at=func.now())
        )
        await session.commit()


async def list_sessions(user_id: str) -> list[dict[str, str]]:
    """当前用户的会话列表（最近活跃在前）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChatSession.id, ChatSession.title, ChatSession.updated_at)
            .where(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc())
            .limit(50)
        )
        return [
            {
                "session_id": sid,
                "title": title,
                "updated_at": updated.isoformat() if updated else "",
            }
            for sid, title, updated in result.all()
        ]


async def delete_session(session_id: str, user_id: str) -> bool:
    """删除会话及其消息（仅属主可删）。返回是否删除成功。"""
    async with AsyncSessionLocal() as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.user_id != user_id:
            return False
        await session.execute(
            delete(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        await session.delete(row)
        await session.commit()
        return True

