"""可观测层查询接口：运行轨迹查询与全局统计。

鉴权口径：/api/runs* 只返回**当前登录用户本人**的记录；
/api/stats 为全局聚合数据（不含问答内容），登录即可查。
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, get_current_user
from app.core.db import AsyncSessionLocal
from app.models.run import AgentRun, LlmCall, ToolCall

logger = logging.getLogger(__name__)
router = APIRouter()


def _run_view(run: AgentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "user_id": run.user_id,
        "question": run.question,
        "answer": run.answer,
        "status": run.status,
        "total_tokens": run.total_tokens,
        "total_cost": run.total_cost,
        "duration_ms": run.duration_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/api/runs/{run_id}")
async def get_run(
    run_id: int, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    """单条对话的完整轨迹：汇总 + LLM 明细 + 工具明细。"""
    async with AsyncSessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is None or run.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="对话记录不存在")

        llm_calls = (
            (
                await session.execute(
                    select(LlmCall).where(LlmCall.run_id == run_id).order_by(LlmCall.id)
                )
            )
            .scalars()
            .all()
        )
        tool_calls = (
            (
                await session.execute(
                    select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)
                )
            )
            .scalars()
            .all()
        )

    return {
        "code": 200,
        "message": "ok",
        "data": {
            "run": _run_view(run),
            "llm_calls": [
                {
                    "id": c.id,
                    "round": c.round,
                    "request_summary": c.request_summary,
                    "response_summary": c.response_summary,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "duration_ms": c.duration_ms,
                }
                for c in llm_calls
            ],
            "tool_calls": [
                {
                    "id": c.id,
                    "tool_name": c.tool_name,
                    "arguments": c.arguments,
                    "result": c.result,
                    "status": c.status,
                    "error": c.error,
                    "duration_ms": c.duration_ms,
                    "idempotency_key": c.idempotency_key,
                }
                for c in tool_calls
            ],
        },
    }


@router.get("/api/runs")
async def list_runs(
    session_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """本人的对话记录列表（按时间倒序），可按会话过滤。"""
    stmt = select(AgentRun).where(AgentRun.user_id == user.user_id)
    if session_id:
        stmt = stmt.where(AgentRun.session_id == session_id)
    stmt = stmt.order_by(AgentRun.id.desc()).limit(limit)

    async with AsyncSessionLocal() as session:
        runs = (await session.execute(stmt)).scalars().all()

    return {
        "code": 200,
        "message": "ok",
        "data": {"total": len(runs), "list": [_run_view(r) for r in runs]},
    }


@router.get("/api/stats")
async def stats(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """全局聚合统计（不含问答内容）：规模 / 成本 / 时延 / 工具分布 / 拒答。"""
    async with AsyncSessionLocal() as session:
        run_row = (
            await session.execute(
                select(
                    func.count(AgentRun.id),
                    func.coalesce(func.sum(AgentRun.total_tokens), 0),
                    func.coalesce(func.sum(AgentRun.total_cost), 0.0),
                    func.coalesce(func.avg(AgentRun.duration_ms), 0.0),
                )
            )
        ).one()
        refused_count = (
            await session.execute(
                select(func.count(AgentRun.id)).where(AgentRun.status == "refused")
            )
        ).scalar_one()

        tool_rows = (
            await session.execute(
                select(ToolCall.tool_name, func.count(ToolCall.id)).group_by(
                    ToolCall.tool_name
                )
            )
        ).all()

    total_runs, total_tokens, total_cost, avg_duration = run_row
    return {
        "code": 200,
        "message": "ok",
        "data": {
            "total_runs": total_runs,
            "total_tokens": total_tokens,
            "total_cost": round(float(total_cost), 6),
            "avg_duration_ms": round(float(avg_duration), 1),
            "refused_count": refused_count,
            "tool_distribution": {name: count for name, count in tool_rows},
        },
    }
