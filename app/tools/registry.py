"""工具注册表：统一维护工具的名称、描述、参数 Schema 与处理函数。

安全约定：`injected` 中列出的参数由服务端注入（例如 user_id），
既不写进 `parameters`（模型看不到、无法指定），也会在调用时忽略模型传的同名字段，
从根上杜绝模型伪造身份造成的越权。

统一拦截：call() 是所有工具执行的唯一入口，在此做参数白名单过滤、
计时与结构化日志（工具名 / 参数 / 结果摘要 / 耗时 / 异常）。
"""
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 结果摘要的截断长度，避免日志被大结果刷屏
LOG_RESULT_PREVIEW_LIMIT = 500

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    """一个可被 LLM 调用的工具。"""

    name: str
    description: str
    # 模型可见的参数 JSON Schema
    parameters: dict[str, Any]
    handler: ToolHandler
    # 由服务端注入、模型不可见的上下文参数名
    injected: tuple[str, ...] = ()

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI tools 字段要求的格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册与分发。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具，重名直接报错，避免覆盖造成隐性行为变化。"""
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        """供 LLM 请求使用的工具定义列表（不含注入参数）。"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        """执行工具。context 为服务端上下文（如当前用户 user_id）。"""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具：{name}")

        context = context or {}
        # 参数白名单：Schema 声明过的字段才允许透传，模型瞎编的参数一律丢弃
        allowed = set((tool.parameters.get("properties") or {}).keys())
        # 先注入服务端上下文，再合并模型参数；模型若传了注入字段一律忽略
        kwargs: dict[str, Any] = {
            key: context[key] for key in tool.injected if key in context
        }
        for key, value in arguments.items():
            if key in tool.injected:
                logger.warning("工具 %s 收到模型传入的注入字段 %s，已忽略", name, key)
                continue
            if key not in allowed:
                logger.warning("工具 %s 收到未声明的参数 %s，已忽略", name, key)
                continue
            kwargs[key] = value

        # 统一拦截：计时 + 结构化日志。
        # 注意：日志只记录模型侧 arguments，绝不记录 kwargs —— kwargs 含注入的
        # user_token / user_id 等敏感上下文，落日志会造成凭证泄漏。
        started = time.perf_counter()
        try:
            result = await tool.handler(**kwargs)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            logger.exception(
                "工具调用异常 tool=%s args=%s latency_ms=%d error=%s",
                name, arguments, latency_ms, exc,
            )
            raise
        latency_ms = round((time.perf_counter() - started) * 1000)
        try:
            result_preview = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            result_preview = str(result)
        logger.info(
            "工具调用完成 tool=%s args=%s latency_ms=%d result=%s",
            name, arguments, latency_ms, result_preview[:LOG_RESULT_PREVIEW_LIMIT],
        )
        return result
