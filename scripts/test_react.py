"""命令行测试 ReAct 循环：输入问题，打印完整的「思考 - 行动 - 观察」轨迹。

用法（在 agent-service/ 目录下）：
    .venv\\Scripts\\python.exe scripts/test_react.py
输入 exit / quit 退出。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.react import ReactResult, run  # noqa: E402


def print_trace(question: str, result: ReactResult) -> None:
    """打印一次 ReAct 的完整轨迹。"""
    print("-" * 70)
    print(f"用户：{question}")
    for step in result.steps:
        print(f"  [第 {step.round} 轮] 思考：{step.thought or '（无文本，直接调用工具）'}")
        print(f"           行动：{step.tool_name}  参数：{step.tool_args}")
        status = "成功" if step.ok else "失败"
        print(f"           观察（{status}，{step.latency_ms}ms）：{step.tool_result[:300]}")
    print(f"  -> 最终回答（{result.rounds} 轮 / {result.latency_ms}ms）：")
    print(f"     {result.answer}")
    print(
        f"  token：prompt={result.prompt_tokens} completion={result.completion_tokens}"
    )


async def ask(question: str) -> None:
    try:
        result = await run(question)
    except Exception as exc:  # noqa: BLE001 - 命令行工具，失败直接提示
        print(f"  !! 本次调用失败：{exc}")
        return
    print_trace(question, result)


async def repl() -> None:
    while True:
        try:
            question = input("\n请输入问题（exit 退出）：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        await ask(question)


async def run_default_cases() -> None:
    """无交互参数时跑两个验收用例。"""
    for question in ["献血后多久可以再次献血", "今天天气怎么样"]:
        await ask(question)


def main() -> None:
    if len(sys.argv) > 1:
        asyncio.run(ask(" ".join(sys.argv[1:])))
    elif sys.stdin.isatty():
        asyncio.run(repl())
    else:
        asyncio.run(run_default_cases())


if __name__ == "__main__":
    main()
