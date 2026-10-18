"""LLM 客户端：OpenAI 兼容协议（智谱 glm，可切百炼），支持普通对话与工具调用。

统一记录 token 用量与耗时；超时 60s。
- chat：非流式，失败重试 3 次、指数退避（测试脚本与批处理场景用）
- chat_stream：流式（SSE 对话页用），产出 content 增量与最终合并结果；
  流式响应已开始产出后无法安全重试，故仅在建立连接阶段重试
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 3


@dataclass
class StreamedFunction:
    """流式增量合并出的工具调用函数体。"""

    name: str = ""
    arguments: str = ""


@dataclass
class StreamedToolCall:
    """流式增量合并出的完整工具调用（与非流式 tool_call 结构对齐）。"""

    id: str = ""
    function: StreamedFunction = field(default_factory=StreamedFunction)

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        """与 openai SDK 对象的序列化口径一致，供 messages 回传使用。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


@dataclass
class LLMUsage:
    """一次调用的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """一次调用的结果。"""

    content: str | None
    tool_calls: list[Any] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    model: str = ""


class LLMClient:
    """对话模型客户端。"""

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=REQUEST_TIMEOUT,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> LLMResponse:
        """发起一次对话补全。tools 非空时启用函数调用。"""
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            started = time.perf_counter()
            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 兜住 SDK 各类异常后重试
                last_error = exc
                wait = 2**attempt
                logger.warning(
                    "LLM 调用第 %d/%d 次失败：%s，%d 秒后重试",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
                continue

            latency_ms = round((time.perf_counter() - started) * 1000)
            message = completion.choices[0].message
            usage_obj = getattr(completion, "usage", None)
            usage = LLMUsage(
                prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
            )
            logger.info(
                "LLM 调用完成 model=%s 耗时=%dms prompt=%d completion=%d tool_calls=%d",
                settings.llm_model,
                latency_ms,
                usage.prompt_tokens,
                usage.completion_tokens,
                len(message.tool_calls or []),
            )
            return LLMResponse(
                content=message.content,
                tool_calls=list(message.tool_calls or []),
                usage=usage,
                latency_ms=latency_ms,
                model=settings.llm_model,
            )

        raise RuntimeError(f"LLM 连续 {MAX_RETRIES} 次调用失败：{last_error}")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式对话补全。

        产出事件序列：
        - {"type": "delta", "text": 文本增量}：content 逐段到达，边到边推
        - {"type": "final", ...}：流结束后的合并结果，含完整 content、
          按 index 合并的 tool_calls、usage 与耗时（整条流最后一条事件）

        重试策略：只在「尚未产出任何内容」时允许重试；一旦开始产出，
        失败直接抛错（已推送的增量无法收回，重试会造成重复输出）。
        """
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            # 要求末帧返回 token 用量；平台不支持时 usage 记 0（仅影响统计精度）
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        started = time.perf_counter()
        stream = None
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                stream = await self._client.chat.completions.create(**kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - 连接建立阶段失败可安全重试
                last_error = exc
                wait = 2**attempt
                logger.warning(
                    "LLM 流式调用第 %d/%d 次建立失败：%s，%d 秒后重试",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
        if stream is None:
            raise RuntimeError(f"LLM 流式连续 {MAX_RETRIES} 次建立失败：{last_error}")

        content_parts: list[str] = []
        # 流式 tool_calls 按 index 增量合并：id/name 只在首帧出现，arguments 分片追加
        tool_slots: dict[int, StreamedToolCall] = {}
        usage = LLMUsage()

        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = LLMUsage(
                    prompt_tokens=getattr(chunk_usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(chunk_usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(chunk_usage, "total_tokens", 0) or 0,
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "delta", "text": delta.content}
            for tc_delta in delta.tool_calls or []:
                slot = tool_slots.setdefault(tc_delta.index, StreamedToolCall())
                if tc_delta.id:
                    slot.id = tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        slot.function.name = tc_delta.function.name
                    if tc_delta.function.arguments:
                        slot.function.arguments += tc_delta.function.arguments

        latency_ms = round((time.perf_counter() - started) * 1000)
        tool_calls = [tool_slots[i] for i in sorted(tool_slots)]
        logger.info(
            "LLM 流式调用完成 model=%s 耗时=%dms prompt=%d completion=%d tool_calls=%d",
            settings.llm_model,
            latency_ms,
            usage.prompt_tokens,
            usage.completion_tokens,
            len(tool_calls),
        )
        yield {
            "type": "final",
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "usage": usage,
            "latency_ms": latency_ms,
        }


# 全局单例
llm = LLMClient()
