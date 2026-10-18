"""工具层：注册表 + 各业务工具。

本步（Step 4）注册全部四个工具：
- search_policy      政策条文检索（RAG）
- query_record       查本人献血记录 / 证书状态（身份由服务端注入）
- list_events        查可报名活动
- create_appointment 生成报名草稿（不含健康征询，确认后才正式报名）

安全约定：user_id / user_token 一律通过 `injected` 由服务端注入，
模型既看不到也无法指定，杜绝越权查询他人数据。
"""
from app.tools.appointment import create_appointment
from app.tools.events import list_events
from app.tools.policy import search_policy
from app.tools.record import query_record
from app.tools.registry import Tool, ToolRegistry

default_registry = ToolRegistry()

default_registry.register(
    Tool(
        name="search_policy",
        description=(
            "检索献血相关政策的原文条款（献血条件、献血间隔、献血前准备与暂缓情形等）。"
            "凡涉及政策、规定、条件、流程的问题都必须先调用本工具；"
            "检索不到依据时如实说明「没有找到相关依据」，不得凭记忆作答。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用于检索的问题或关键词，尽量保留用户原话中的关键信息",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条文条数上限，默认 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=search_policy,
    )
)

default_registry.register(
    Tool(
        name="query_record",
        description=(
            "查询当前登录用户本人的献血记录与证书状态。"
            "不传 record_no 时返回个人献血摘要（认证状态、献血次数、总量、能否再次献血）"
            "和最近记录列表；传入 record_no 时返回该编号的证书状态与链上验真结果。"
            "只能查当前用户自己的数据，不需要也不接受任何用户标识参数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "record_no": {
                    "type": "string",
                    "description": "献血编号（BD 开头），仅当用户明确给出时传入",
                },
            },
            "required": [],
        },
        handler=query_record,
        injected=("user_token",),
    )
)

default_registry.register(
    Tool(
        name="list_events",
        description=(
            "查询当前可报名的献血活动列表（时间、地点、状态）。"
            "用户询问近期有哪些献血活动、想报名时使用。"
            "日期参数都可不传：不传时返回全部可报名活动；"
            "只有用户明确给出日期范围时才传 from_date / to_date，不要自己猜日期。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "起始日期 YYYY-MM-DD（含当天），按活动开始日期过滤，可不传",
                },
                "to_date": {
                    "type": "string",
                    "description": "结束日期 YYYY-MM-DD（含当天），按活动开始日期过滤，可不传",
                },
            },
            "required": [],
        },
        handler=list_events,
        injected=("user_token",),
    )
)

default_registry.register(
    Tool(
        name="create_appointment",
        description=(
            "为当前用户生成献血活动的【报名草稿】。"
            "注意：本工具只生成草稿，不代表报名成功；"
            "草稿需用户本人在确认卡片中亲自填写健康征询并同意知情同意后，才会正式提交报名。"
            "健康征询与知情同意绝不能由你代答或代填，也不要向用户索取这些信息。"
            "调用前必须先通过 list_events 确认活动真实存在，event_id 必须来自 list_events 的返回。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "integer",
                    "description": "献血活动 id，必须来自 list_events 返回的活动 id",
                },
                "donation_type": {
                    "type": "string",
                    "enum": ["whole_blood", "component_blood"],
                    "description": "献血类型：whole_blood（全血）或 component_blood（成分血）",
                },
            },
            "required": ["event_id", "donation_type"],
        },
        handler=create_appointment,
        injected=("user_id", "user_token"),
    )
)

__all__ = ["Tool", "ToolRegistry", "default_registry"]
