"""一键灌库脚本：把 data/policy_docs/ 下的政策文档切片、向量化并写入 pgvector。

用法（在 agent-service/ 目录下）：
    .venv\\Scripts\\python.exe scripts/ingest_policy.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.config import BASE_DIR  # noqa: E402
from app.core.db import AsyncSessionLocal, engine  # noqa: E402
from app.models.policy import PolicyChunk  # noqa: E402
from app.rag.embedder import embedder  # noqa: E402
from app.rag.ingest import ingest_documents, read_documents  # noqa: E402


async def main() -> None:
    docs_dir = BASE_DIR / "data" / "policy_docs"
    documents = read_documents(docs_dir)
    if not documents:
        print(f"未在 {docs_dir} 找到 .md / .txt 文档，请先放入政策文件。")
        return

    print(f"待入库文档 {len(documents)} 个，开始切片与向量化…")
    result = await ingest_documents(documents, embedder)

    for source_path, chunk_count in result["documents"]:
        print(f"  - {source_path}: {chunk_count} 块")

    async with AsyncSessionLocal() as session:
        total_rows = (await session.execute(select(func.count(PolicyChunk.id)))).scalar()
    print(f"本次写入 {result['total_chunks']} 块；库中当前共 {total_rows} 块。")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
