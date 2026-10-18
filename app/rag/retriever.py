"""向量检索：问题向量化 → pgvector 余弦相似度 top-k → 阈值过滤。

本层只做纯向量检索，不接 LLM、不做重排序。
"""
import logging

from sqlalchemy import select

from app.config import settings
from app.core.db import AsyncSessionLocal
from app.models.policy import PolicyChunk
from app.rag.embedder import Embedder, embedder

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5


async def retrieve(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    threshold: float | None = None,
    embedder_impl: Embedder | None = None,
) -> list[dict]:
    """检索政策条文。

    Args:
        query: 用户问题。
        top_k: 取相似度最高的前 k 条。
        threshold: 最低相似度，低于该分数的结果被丢弃；默认取配置值。

    Returns:
        [{content, doc_title, section, source_path, score}]，按分数降序。
        全部低于阈值时返回空列表（调用方不得编造答案）。
    """
    if not query or not query.strip():
        return []

    min_score = settings.retrieval_score_threshold if threshold is None else threshold
    emb = embedder_impl or embedder
    query_vector = await emb.embed_one(query.strip())

    distance = PolicyChunk.embedding.cosine_distance(query_vector)
    statement = select(PolicyChunk, distance.label("distance")).order_by(distance).limit(
        top_k
    )

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(statement)).all()

    results: list[dict] = []
    for chunk, dist in rows:
        # pgvector 余弦距离 = 1 - 余弦相似度
        score = 1.0 - float(dist)
        if score < min_score:
            continue
        results.append(
            {
                "content": chunk.content,
                "doc_title": chunk.doc_title,
                "section": chunk.section,
                "source_path": chunk.source_path,
                "score": round(score, 4),
            }
        )

    logger.info(
        "检索 query=%r 命中 %d/%d 条（阈值 %.2f）", query, len(results), len(rows), min_score
    )
    return results
