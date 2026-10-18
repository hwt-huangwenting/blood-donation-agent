"""对话接口（SSE 流式）：用户消息 → ReAct 循环 → 逐事件推送。

事件协议（每行一个 SSE data，JSON 序列化）：
- delta        答案/思考文本增量（逐字流式渲染）
- tool_call    工具调用开始（工具名、参数、轮次）
- tool_result  工具返回（结果截断、耗时、成功与否）
- citations    引用来源（结构化，含原文片段）
- confirm_card 报名草稿已生成（前端弹确认卡片，用户本人填写后调 confirm 接口）
- done         结束（含 run_id / 完整答案 / 状态）
- error        主流程异常

说明：EventSource 无法自定义 Authorization 头，前端用 fetch + ReadableStream 消费。
"""
import json
import logging
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.memory import (
    append_message,
    delete_session,
    get_or_create_session,
    list_sessions,
    load_history,
)
from app.agent.react import run_stream
from app.api.deps import CurrentUser, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    """对话请求体。session_id 缺省时由服务端生成新会话（经 done 事件带回）。"""

    message: str
    session_id: str | None = None


def _sse(event: dict[str, Any]) -> str:
    """序列化一条 SSE data 行。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/api/chat")
async def chat(req: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    message = req.message.strip()
    if not message:
        # 请求体级错误：还没进入流式阶段，直接返回 400 JSON
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = (req.session_id or "").strip() or uuid.uuid4().hex
    await get_or_create_session(session_id, user.user_id)
    history = await load_history(session_id, user.user_id)
    # 累积答案全文，用于 done 时写入会话记忆（PG 持久化）
    answer_parts: list[str] = []

    async def event_source() -> AsyncGenerator[str, None]:
        try:
            async for event in run_stream(
                message,
                history=history,
                user_id=user.user_id,
                user_token=user.token,
                session_id=session_id,
            ):
                if event.get("type") == "delta":
                    answer_parts.append(event.get("text", ""))
                if event.get("type") == "done":
                    event["session_id"] = session_id
                    answer = event.get("answer") or "".join(answer_parts)
                    await append_message(session_id, user.user_id, "user", message)
                    await append_message(session_id, user.user_id, "assistant", answer)
                yield _sse(event)
        except Exception:
            # run_stream 内部已兜底，这里是最后防线（如序列化异常）
            logger.exception("SSE 推送异常 session=%s", session_id)
            yield _sse({"type": "error", "message": "对话处理失败，请稍后重试"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 禁用反向代理缓冲，保证事件即时到达前端
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/chat/sessions")
async def get_sessions(user: CurrentUser = Depends(get_current_user)):
    """当前用户的会话列表（最近活跃在前）。"""
    return {"sessions": await list_sessions(user.user_id)}


@router.get("/api/chat/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str, user: CurrentUser = Depends(get_current_user)
):
    """会话历史消息（仅属主可见；他人会话返回空列表，不暴露存在性）。"""
    return {"messages": await load_history(session_id, user.user_id)}


@router.delete("/api/chat/sessions/{session_id}")
async def remove_session(
    session_id: str, user: CurrentUser = Depends(get_current_user)
):
    """删除会话及其全部消息（仅属主可删）。"""
    ok = await delete_session(session_id, user.user_id)
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}
