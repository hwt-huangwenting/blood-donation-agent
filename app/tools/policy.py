"""工具：search_policy —— 政策条文检索（RAG）。

只返回库中检索到的原文；检索不到就返回空列表，绝不编造。
"""
from app.rag.retriever import retrieve

DEFAULT_TOP_K = 5


async def search_policy(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """按问题检索献血政策原文。

    Args:
        query: 用户关于献血政策的问题。
        top_k: 返回条数上限。

    Returns:
        [{content, doc_title, section, source_path, score}]；无依据时为空列表。
    """
    return await retrieve(query, top_k=top_k)
