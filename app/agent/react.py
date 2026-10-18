"""手写 ReAct 循环：理解问题 → 自主选择工具 → 执行 → 观察 → 直到给出答案。

安全约定：user_id 只从服务端上下文注入（context），不作为模型可见的工具参数。
可观测：每次对话的 LLM 调用、工具调用与汇总统一落库（core/observe.py），
埋点在本层完成——这里天然持有轮次、模型侧参数与耗时，且参数不含注入字段。
本步先不做流式输出。
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncGenerator

from app.agent.llm import LLMClient, llm
from app.agent.prompts import FALLBACK_REPLY, SYSTEM_PROMPT
from app.config import settings
from app.core.observe import log_run
from app.tools import ToolRegistry, default_registry

logger = logging.getLogger(__name__)

# 单个问题最多允许的工具调用轮次
MAX_ROUNDS = 5
# 传给模型的历史消息条数上限
MAX_HISTORY_MESSAGES = 20
# 工具结果写入轨迹前的截断长度，避免日志与轨迹视图过长
RESULT_PREVIEW_LIMIT = 2000
# 落库的请求/响应摘要截断长度
SUMMARY_LIMIT = 500


@dataclass
class ReactStep:
    """一轮「思考 - 行动 - 观察」。"""

    round: int
    thought: str | None
    tool_name: str
    tool_args: dict[str, Any]
    tool_result: str
    latency_ms: int
    ok: bool = True


@dataclass
class ReactResult:
    """一次完整回答的过程与结果。"""

    answer: str
    steps: list[ReactStep] = field(default_factory=list)
    rounds: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    # 可观测：本次对话落库后的 run id（落库失败为 None）
    run_id: int | None = None
    # 对话状态：ok / refused / fallback
    status: str = "ok"
    # 引用溯源：本轮 search_policy 命中的条文出处（结构化，不进正文）
    citations: list[dict[str, Any]] = field(default_factory=list)


def _collect_citations(tool_name: str, result: Any, citations: list[dict[str, Any]]) -> None:
    """从 search_policy 的完整结果中提取出处，按（文档, 章节）去重。"""
    if tool_name != "search_policy" or not isinstance(result, list):
        return
    seen = {(c.get("doc_title"), c.get("section")) for c in citations}
    for item in result:
        if not isinstance(item, dict):
            continue
        key = (item.get("doc_title"), item.get("section"))
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "doc_title": item.get("doc_title", ""),
                "section": item.get("section", ""),
                "source_path": item.get("source_path", ""),
                "score": item.get("score"),
                # 原文片段（截断），供前端「点击展开原文」展示
                "content": str(item.get("content", ""))[:200],
            }
        )


def _build_messages(
    user_message: str, history: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """组装 system + history + user 消息序列（流式/非流式共用）。"""
    # 小模型没有当前日期概念，容易给日期参数瞎编值；
    # system 里注入今天日期，作为「最近」「近期」等相对时间的基准
    system_content = (
        f"{SYSTEM_PROMPT}\n\n## 当前环境\n"
        f"- 今天的日期：{date.today().isoformat()}"
        "（涉及「最近」「近期」「这个月」等相对时间时，以此日期为基准推算；"
        "不确定日期范围时，调用工具不要传日期参数）"
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history[-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_message})
    return messages


def _calc_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按配置单价核算本次对话成本（免费模型为 0）。"""
    return (
        prompt_tokens / 1_000_000 * settings.llm_input_price_per_million
        + completion_tokens / 1_000_000 * settings.llm_output_price_per_million
    )


async def run(
    user_message: str,
    *,
    history: list[dict[str, Any]] | None = None,
    user_id: str = "",
    user_token: str = "",
    session_id: str = "",
    registry: ToolRegistry | None = None,
    llm_client: LLMClient | None = None,
) -> ReactResult:
    """执行一轮 ReAct。

    Args:
        user_message: 用户本轮消息。
        history: 会话历史（OpenAI messages 格式，仅含 user/assistant 文本消息）。
        user_id: 服务端注入的当前用户标识，只进 context，不进工具参数。
        user_token: 服务端注入的用户 JWT，供工具以本人身份调用业务接口。
        session_id: 会话标识，用于可观测记录串联。
        registry: 工具注册表，默认用内置注册表。
        llm_client: LLM 客户端，便于测试时替换。
    """
    tool_registry = registry or default_registry
    client = llm_client or llm
    tools_schema = tool_registry.openai_tools()
    # 服务端上下文：身份类信息只从这里注入工具，模型不可见、不可指定
    tool_context = {"user_id": user_id, "user_token": user_token}

    # 消息组装与流式入口共用同一实现，避免两处口径漂移
    messages = _build_messages(user_message, history)

    steps: list[ReactStep] = []
    citations: list[dict[str, Any]] = []
    llm_call_logs: list[dict[str, Any]] = []
    tool_call_logs: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    started = time.perf_counter()

    async def _finish(answer: str, rounds: int, status: str) -> ReactResult:
        """统一收尾：汇总指标、落库（失败不影响返回）、构造结果。"""
        latency = round((time.perf_counter() - started) * 1000)
        total_cost = (
            prompt_tokens / 1_000_000 * settings.llm_input_price_per_million
            + completion_tokens / 1_000_000 * settings.llm_output_price_per_million
        )
        run_id = await log_run(
            session_id=session_id,
            user_id=user_id,
            question=user_message,
            answer=answer,
            status=status,
            total_tokens=prompt_tokens + completion_tokens,
            total_cost=total_cost,
            duration_ms=latency,
            llm_calls=llm_call_logs,
            tool_calls=tool_call_logs,
        )
        return ReactResult(
            answer=answer,
            steps=steps,
            rounds=rounds,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            run_id=run_id,
            status=status,
            citations=citations,
        )

    try:
        for round_index in range(1, MAX_ROUNDS + 1):
            llm_started = time.perf_counter()
            response = await client.chat(messages, tools=tools_schema)
            llm_latency = round((time.perf_counter() - llm_started) * 1000)
            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens

            # 本轮 LLM 明细：请求摘要取最后一条消息，响应摘要取文本或工具调用名
            last_msg = messages[-1]
            if response.tool_calls:
                response_summary = "tool_calls: " + ", ".join(
                    tc.function.name for tc in response.tool_calls
                )
            else:
                response_summary = (response.content or "").strip()
            llm_call_logs.append(
                {
                    "round": round_index,
                    "request_summary": f"{last_msg.get('role', '?')}: "
                    f"{str(last_msg.get('content', ''))[:SUMMARY_LIMIT]}",
                    "response_summary": response_summary[:SUMMARY_LIMIT],
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "duration_ms": llm_latency,
                }
            )

            # 没有工具调用 → 本轮即为最终答案
            if not response.tool_calls:
                # 拒答口径：第一轮未调任何工具直接作答（政策问题按 Prompt 必须先检索，
                # 能直接答的大多为拒答/闲聊场景）
                status = "refused" if round_index == 1 and not steps else "ok"
                return await _finish(
                    (response.content or "").strip(), round_index, status
                )

            # 把助手这一轮的工具调用回合按协议原样回传
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        tool_call.model_dump(exclude_none=True)
                        for tool_call in response.tool_calls
                    ],
                }
            )

            for tool_call in response.tool_calls:
                tool_name = tool_call.function.name
                raw_arguments = tool_call.function.arguments or "{}"
                arguments: dict[str, Any] = {}
                ok = True
                error_text = ""
                result: Any = None
                tool_started = time.perf_counter()

                try:
                    arguments = json.loads(raw_arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                except (json.JSONDecodeError, ValueError) as exc:
                    ok = False
                    error_text = f"工具参数解析失败：{exc}"
                    result_text = f"{error_text}（原始参数：{raw_arguments}）"
                    logger.warning("工具 %s 参数解析失败：%s", tool_name, raw_arguments)
                else:
                    try:
                        result = await tool_registry.call(
                            tool_name, arguments, context=tool_context
                        )
                        result_text = json.dumps(result, ensure_ascii=False)
                        _collect_citations(tool_name, result, citations)
                    except Exception as exc:  # noqa: BLE001 - 工具失败要回灌给模型而不是中断对话
                        ok = False
                        error_text = f"工具执行失败：{exc}"
                        result_text = error_text
                        logger.exception("工具 %s 执行失败", tool_name)

                latency_ms = round((time.perf_counter() - tool_started) * 1000)
                steps.append(
                    ReactStep(
                        round=round_index,
                        thought=response.content or None,
                        tool_name=tool_name,
                        tool_args=arguments,
                        tool_result=result_text[:RESULT_PREVIEW_LIMIT],
                        latency_ms=latency_ms,
                        ok=ok,
                    )
                )
                # 工具明细落库：arguments 为模型侧参数（不含注入字段）；
                # idempotency_key 从写类工具结果中提取，便于核对幂等行为
                idem_key = ""
                if isinstance(result, dict):
                    idem_key = str(result.get("idempotency_key") or "")
                tool_call_logs.append(
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": result if ok else None,
                        "status": "ok" if ok else "error",
                        "error": error_text,
                        "duration_ms": latency_ms,
                        "idempotency_key": idem_key,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    }
                )

        # 超过最大轮次仍未收敛 → 兜底
        logger.warning("ReAct 达到最大轮次 %d 仍未收敛，返回兜底回复", MAX_ROUNDS)
        return await _finish(FALLBACK_REPLY, MAX_ROUNDS, "fallback")
    except Exception:
        # 主流程异常（如 LLM 多次重试后仍失败）：尽力落一条 error 记录后继续上抛
        await _finish("", 0, "error")
        raise


async def run_stream(
    user_message: str,
    *,
    history: list[dict[str, Any]] | None = None,
    user_id: str = "",
    user_token: str = "",
    session_id: str = "",
    registry: ToolRegistry | None = None,
    llm_client: LLMClient | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """流式 ReAct：与 run() 同逻辑，但按 SSE 事件逐步产出。

    事件序列（均为 dict，由 API 层序列化为 SSE data 行）：
    - {"type": "delta", "text", "round"}        答案/思考文本增量
    - {"type": "tool_call", "name", "arguments", "round"}   工具调用开始
    - {"type": "tool_result", "name", "ok", "result", "latency_ms", "round"}
    - {"type": "confirm_card", "draft"}         生成报名草稿（前端弹确认卡片）
    - {"type": "citations", "items"}            引用出处（最终答案轮）
    - {"type": "done", "run_id", "answer", "status"}
    - {"type": "error", "message"}              主流程异常（尽力落库后给出）
    """
    tool_registry = registry or default_registry
    client = llm_client or llm
    tools_schema = tool_registry.openai_tools()
    tool_context = {"user_id": user_id, "user_token": user_token}
    messages = _build_messages(user_message, history)

    citations: list[dict[str, Any]] = []
    llm_call_logs: list[dict[str, Any]] = []
    tool_call_logs: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    started = time.perf_counter()

    async def _finish(answer: str, status: str) -> int | None:
        latency = round((time.perf_counter() - started) * 1000)
        return await log_run(
            session_id=session_id,
            user_id=user_id,
            question=user_message,
            answer=answer,
            status=status,
            total_tokens=prompt_tokens + completion_tokens,
            total_cost=_calc_cost(prompt_tokens, completion_tokens),
            duration_ms=latency,
            llm_calls=llm_call_logs,
            tool_calls=tool_call_logs,
        )

    try:
        for round_index in range(1, MAX_ROUNDS + 1):
            final: dict[str, Any] | None = None
            async for event in client.chat_stream(messages, tools=tools_schema):
                if event["type"] == "delta":
                    yield {"type": "delta", "text": event["text"], "round": round_index}
                else:
                    final = event
            if final is None:
                raise RuntimeError("LLM 流式调用未返回最终结果")

            usage = final["usage"]
            prompt_tokens += usage.prompt_tokens
            completion_tokens += usage.completion_tokens
            last_msg = messages[-1]
            tool_calls = final["tool_calls"]
            response_summary = (
                "tool_calls: " + ", ".join(tc.function.name for tc in tool_calls)
                if tool_calls
                else (final["content"] or "").strip()
            )
            llm_call_logs.append(
                {
                    "round": round_index,
                    "request_summary": f"{last_msg.get('role', '?')}: "
                    f"{str(last_msg.get('content', ''))[:SUMMARY_LIMIT]}",
                    "response_summary": response_summary[:SUMMARY_LIMIT],
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "duration_ms": final["latency_ms"],
                }
            )

            # 没有工具调用 → 本轮即为最终答案
            if not tool_calls:
                answer = (final["content"] or "").strip()
                status = "refused" if round_index == 1 and not tool_call_logs else "ok"
                if citations:
                    yield {"type": "citations", "items": citations}
                run_id = await _finish(answer, status)
                yield {
                    "type": "done",
                    "run_id": run_id,
                    "answer": answer,
                    "status": status,
                }
                return

            # 助手的工具调用回合按协议原样回传
            messages.append(
                {
                    "role": "assistant",
                    "content": final["content"] or "",
                    "tool_calls": [tc.model_dump(exclude_none=True) for tc in tool_calls],
                }
            )

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                raw_arguments = tool_call.function.arguments or "{}"
                arguments: dict[str, Any] = {}
                ok = True
                error_text = ""
                result: Any = None
                tool_started = time.perf_counter()

                try:
                    arguments = json.loads(raw_arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                except (json.JSONDecodeError, ValueError) as exc:
                    ok = False
                    error_text = f"工具参数解析失败：{exc}"
                    result_text = f"{error_text}（原始参数：{raw_arguments}）"
                    logger.warning("工具 %s 参数解析失败：%s", tool_name, raw_arguments)
                else:
                    try:
                        result = await tool_registry.call(
                            tool_name, arguments, context=tool_context
                        )
                        result_text = json.dumps(result, ensure_ascii=False)
                        _collect_citations(tool_name, result, citations)
                    except Exception as exc:  # noqa: BLE001 - 工具失败回灌模型而不是中断
                        ok = False
                        error_text = f"工具执行失败：{exc}"
                        result_text = error_text
                        logger.exception("工具 %s 执行失败", tool_name)

                latency_ms = round((time.perf_counter() - tool_started) * 1000)
                yield {
                    "type": "tool_call",
                    "name": tool_name,
                    "arguments": arguments,
                    "round": round_index,
                }
                yield {
                    "type": "tool_result",
                    "name": tool_name,
                    "ok": ok,
                    "result": result_text[:RESULT_PREVIEW_LIMIT],
                    "latency_ms": latency_ms,
                    "round": round_index,
                }

                # 报名草稿生成成功 → 立即通知前端弹确认卡片（不等对话结束）
                if tool_name == "create_appointment" and ok:
                    try:
                        payload = json.loads(result_text)
                    except json.JSONDecodeError:
                        payload = {}
                    if isinstance(payload, dict) and payload.get("ok"):
                        yield {"type": "confirm_card", "draft": payload.get("draft")}

                idem_key = ""
                if isinstance(result, dict):
                    idem_key = str(result.get("idempotency_key") or "")
                tool_call_logs.append(
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": result if ok else None,
                        "status": "ok" if ok else "error",
                        "error": error_text,
                        "duration_ms": latency_ms,
                        "idempotency_key": idem_key,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    }
                )

        # 超过最大轮次仍未收敛 → 兜底
        logger.warning("ReAct 流式达到最大轮次 %d 仍未收敛，返回兜底回复", MAX_ROUNDS)
        yield {"type": "delta", "text": FALLBACK_REPLY, "round": MAX_ROUNDS}
        run_id = await _finish(FALLBACK_REPLY, "fallback")
        yield {
            "type": "done",
            "run_id": run_id,
            "answer": FALLBACK_REPLY,
            "status": "fallback",
        }
    except Exception as exc:  # noqa: BLE001 - 异常转成 error 事件推给前端
        logger.exception("ReAct 流式主流程异常")
        await _finish("", "error")
        yield {"type": "error", "message": f"对话处理失败：{exc}"}
