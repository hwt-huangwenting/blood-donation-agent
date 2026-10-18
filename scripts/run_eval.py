"""评测脚本：跑 eval_cases.json 全量用例，产出指标报告 eval_report.md。

判定全部使用确定性规则（工具调用比对、引用文档名命中、关键词匹配），
不使用 LLM 当裁判，保证结果可复现。

两类执行路径：
- 内部用例（40 条）：直接调 agent.react.run()，注入测试用户身份，
  拿到工具轨迹 / 引用 / token 用量做判定；
- HTTP 用例（3 条越权）：走真实 POST /api/chat（SSE 流），
  覆盖鉴权链路（无 token 401、越权不泄露他人数据）。

运行方式（agent-service/ 目录下，需 8000 与 3100 在线）：
    .venv/Scripts/python.exe scripts/run_eval.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

# 保证能 import app 包（脚本位于 agent-service/scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.react import run  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent.parent / "data" / "eval_cases.json"
REPORT_PATH = Path(__file__).resolve().parent.parent / "docs" / "eval_report.md"

# 评测身份：it009（donor，user_id=35，0 次献血记录）
EVAL_USER_ID = "35"
EVAL_USERNAME = "it009"
EVAL_PASSWORD = "123456"
BIZ_LOGIN_URL = "http://localhost:3100/api/auth/login"
AGENT_CHAT_URL = "http://localhost:8000/api/chat"


async def get_eval_token() -> str:
    """登录业务系统拿评测用户的真实 JWT。"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            BIZ_LOGIN_URL, json={"username": EVAL_USERNAME, "password": EVAL_PASSWORD}
        )
        resp.raise_for_status()
        return resp.json()["data"]["token"]


def check_keywords(answer: str, case: dict) -> tuple[bool, str]:
    """关键词规则判定，返回 (是否通过, 失败原因)。"""
    for kw in case.get("answer_must_include", []):
        if kw not in answer:
            return False, f"答案缺少必含关键词「{kw}」"
    any_list = case.get("answer_must_include_any", [])
    if any_list and not any(kw in answer for kw in any_list):
        return False, f"答案未命中任一期望关键词 {any_list}"
    for kw in case.get("answer_must_not_include", []):
        if kw in answer:
            return False, f"答案含禁忌关键词「{kw}」"
    return True, ""


def judge_internal(case: dict, result) -> tuple[bool, str]:
    """内部用例判定：工具调用 + 引用文档 + 关键词。"""
    called_tools = [s.tool_name for s in result.steps]
    expect_tools = case.get("expect_tools")

    if expect_tools is not None:
        if not expect_tools:
            # 拒答场景：不允许调用任何工具
            if called_tools:
                return False, f"期望零工具调用，实际调用了 {called_tools}"
        else:
            for t in expect_tools:
                if t not in called_tools:
                    return False, f"期望调用工具 {t}，实际调用 {called_tools}"

    # 检索命中：期望文档必须出现在引用里
    expect_docs = case.get("expect_docs", [])
    if expect_docs:
        cited_docs = {c.get("doc_title", "") for c in result.citations}
        for doc in expect_docs:
            if doc not in cited_docs:
                return False, f"引用中未命中期望文档《{doc}》，实际引用 {cited_docs}"

    return check_keywords(result.answer, case)


async def run_http_case(case: dict, token: str) -> dict:
    """越权用例：走真实 SSE 接口。token=self 用评测用户 token，none 不带。"""
    headers = {}
    if case.get("token") == "self":
        headers["Authorization"] = f"Bearer {token}"

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60) as client:
        if case.get("expect_http_status"):
            # 预期直接拒绝（如 401），不会进入 SSE 流
            resp = await client.post(
                AGENT_CHAT_URL, json={"message": case["question"]}, headers=headers
            )
            latency = round((time.perf_counter() - started) * 1000)
            ok = resp.status_code == case["expect_http_status"]
            return {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "ok": ok,
                "reason": "" if ok else f"期望 HTTP {case['expect_http_status']}，实际 {resp.status_code}",
                "answer": resp.text[:200],
                "tools": [],
                "latency_ms": latency,
                "tokens": 0,
            }

        # 正常 SSE 流：收集 delta 拼完整答案，done 拿状态
        answer_parts: list[str] = []
        tools_called: list[str] = []
        async with client.stream(
            "POST", AGENT_CHAT_URL, json={"message": case["question"]}, headers=headers
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "delta":
                    answer_parts.append(event.get("text", ""))
                elif event.get("type") == "tool_call":
                    tools_called.append(event.get("tool", ""))
        latency = round((time.perf_counter() - started) * 1000)
        answer = "".join(answer_parts)

    ok, reason = check_keywords(answer, case)
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "ok": ok,
        "reason": reason,
        "answer": answer[:200],
        "tools": tools_called,
        "latency_ms": latency,
        "tokens": 0,  # HTTP 路径拿不到 token 明细，由 agent_runs 表可查
    }


async def run_internal_case(case: dict, token: str) -> dict:
    """内部用例：直接跑 ReAct 循环，拿完整轨迹。"""
    result = await run(
        case["question"],
        user_id=EVAL_USER_ID,
        user_token=token,
        session_id=f"eval-{case['id']}",
    )
    ok, reason = judge_internal(case, result)
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "ok": ok,
        "reason": reason,
        "answer": result.answer[:200],
        "tools": [s.tool_name for s in result.steps],
        "citations": [c.get("doc_title") for c in result.citations],
        "latency_ms": result.latency_ms,
        "tokens": result.prompt_tokens + result.completion_tokens,
    }


def build_report(rows: list[dict], total_latency: int, total_tokens: int) -> str:
    """汇总指标并渲染 Markdown 报告。"""
    cats: dict[str, list[dict]] = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)

    def acc(cat: str) -> str:
        items = cats.get(cat, [])
        if not items:
            return "-"
        passed = sum(1 for i in items if i["ok"])
        return f"{passed}/{len(items)}（{passed / len(items) * 100:.0f}%）"

    failed = [r for r in rows if not r["ok"]]
    lines = [
        "# Agent 评测报告",
        "",
        f"- 用例总数：{len(rows)}",
        f"- 总耗时：{total_latency / 1000:.1f}s，平均 {total_latency / len(rows) / 1000:.1f}s/条",
        f"- Token 总量：{total_tokens}（glm-4-flash 免费档，成本 ¥0）",
        "",
        "## 分类指标",
        "",
        "| 维度 | 准确率 |",
        "|---|---|",
        f"| 工具选择-政策问答（search_policy） | {acc('policy')} |",
        f"| 工具选择-记录查询（query_record） | {acc('record')} |",
        f"| 工具选择-活动报名（list_events/create_appointment） | {acc('events')} |",
        f"| 检索命中率（引用文档命中） | {_doc_hit_rate(cats.get('policy', []))} |",
        f"| 拒答准确率（零工具+引导回血站主题） | {acc('refuse')} |",
        f"| 医疗引导合规率（检索+引导语，不下结论） | {acc('medical')} |",
        f"| 越权拦截率（401/不泄露他人数据） | {acc('auth')} |",
        "",
        f"**整体通过率：{sum(1 for r in rows if r['ok'])}/{len(rows)}**",
        "",
    ]
    if failed:
        lines += ["## 失败用例", ""]
        for r in failed:
            lines.append(f"- [{r['id']}] {r['question']}")
            lines.append(f"  - 原因：{r['reason']}")
            lines.append(f"  - 工具：{r.get('tools')}；答案摘要：{r['answer'][:100]}")
        lines.append("")
    lines += [
        "## 全量明细",
        "",
        "| id | 类别 | 通过 | 工具 | 耗时ms | token | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['category']} | {'✅' if r['ok'] else '❌'} "
            f"| {','.join(r.get('tools') or []) or '-'} | {r['latency_ms']} | {r['tokens']} "
            f"| {r['reason'] or ''} |"
        )
    lines.append("")
    return "\n".join(lines)


def _doc_hit_rate(policy_rows: list[dict]) -> str:
    """政策用例中引用文档命中率（单独统计检索质量）。"""
    if not policy_rows:
        return "-"
    hits = sum(
        1
        for r in policy_rows
        if "未命中期望文档" not in r["reason"]
        and r.get("citations")
    )
    return f"{hits}/{len(policy_rows)}（{hits / len(policy_rows) * 100:.0f}%）"


async def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    token = await get_eval_token()
    print(f"已获取评测 token（{EVAL_USERNAME}），共 {len(cases)} 条用例\n")

    rows: list[dict] = []
    for i, case in enumerate(cases, 1):
        try:
            if case.get("mode") == "http":
                row = await run_http_case(case, token)
            else:
                row = await run_internal_case(case, token)
        except Exception as exc:  # 单条异常不中断整轮评测
            row = {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "ok": False,
                "reason": f"执行异常：{exc}",
                "answer": "",
                "tools": [],
                "latency_ms": 0,
                "tokens": 0,
            }
        rows.append(row)
        mark = "PASS" if row["ok"] else "FAIL"
        print(f"[{i}/{len(cases)}] {mark} {row['id']} {row['reason']}")

    total_latency = sum(r["latency_ms"] for r in rows)
    total_tokens = sum(r["tokens"] for r in rows)
    report = build_report(rows, total_latency, total_tokens)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\n报告已写入 {REPORT_PATH}")
    print(f"整体通过：{sum(1 for r in rows if r['ok'])}/{len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
