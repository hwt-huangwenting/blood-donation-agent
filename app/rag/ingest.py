"""政策文档切片与入库。

切片策略：先按 Markdown 标题层级 + 段落拆块，再把段落贪心打包成
400-600 字的语义块，相邻块之间保留约 80 字重叠，避免答案卡在边界上。
"""
import re
from pathlib import Path

from sqlalchemy import delete

from app.config import BASE_DIR
from app.core.db import AsyncSessionLocal
from app.models.policy import PolicyChunk
from app.rag.embedder import Embedder, embedder

# 块长度按字符数计（中文一字一字符）
CHUNK_MIN = 400
CHUNK_MAX = 600
CHUNK_OVERLAP = 80

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def iter_blocks(text: str) -> list[tuple[str, str]]:
    """按标题层级 + 段落拆块，返回 [(章节路径, 段落内容)]。"""
    stack: list[tuple[int, str]] = []
    blocks: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = HEADING_RE.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2).strip()
            # 同级或更深的标题出栈，保证章节路径正确
            stack = [(lv, t) for lv, t in stack if lv < level]
            stack.append((level, title))
            continue
        blocks.append((" / ".join(t for _, t in stack), line))
    return blocks


def split_text(
    text: str,
    max_len: int = CHUNK_MAX,
    min_len: int = CHUNK_MIN,
    overlap: int = CHUNK_OVERLAP,
) -> list[tuple[str, str]]:
    """把整篇文档切成 [(章节路径, 块内容)]。

    块内可能跨小节：章节路径取块内字符占比最大的小节，保证引用展示不失真。
    """
    chunks: list[tuple[str, str]] = []
    buffer = ""
    section_chars: dict[str, int] = {}

    def flush() -> None:
        """把当前缓冲落盘，并清空缓冲。"""
        nonlocal buffer, section_chars
        if not buffer:
            return
        label = (
            max(section_chars, key=lambda key: section_chars[key])
            if section_chars
            else ""
        )
        chunks.append((label, buffer))
        buffer = ""
        section_chars = {}

    for section, paragraph in iter_blocks(text):
        # 单段就超长：先把缓冲落盘，再按窗口硬切该段
        if len(paragraph) > max_len:
            flush()
            step = max_len - overlap
            for start in range(0, len(paragraph), step):
                piece = paragraph[start : start + max_len]
                chunks.append((section, piece))
                if start + max_len >= len(paragraph):
                    break
            continue

        if buffer and len(buffer) + 1 + len(paragraph) > max_len:
            if len(buffer) >= min_len:
                tail = buffer[-overlap:]
                flush()
                buffer = tail
                section_chars = {section: len(tail)}
            # 缓冲不足 min_len 时直接并入，允许轻微超长

        buffer = f"{buffer}\n{paragraph}" if buffer else paragraph
        section_chars[section] = section_chars.get(section, 0) + len(paragraph)

    flush()
    return chunks


def read_documents(docs_dir: str | Path) -> list[dict]:
    """读取目录下所有 .md / .txt，返回待入库的文档结构。"""
    docs_path = Path(docs_dir)
    files = sorted([*docs_path.glob("*.md"), *docs_path.glob("*.txt")])
    documents: list[dict] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        title = _first_h1(text) or path.stem
        chunks = split_text(text)
        documents.append(
            {
                "doc_title": title,
                "source_path": f"data/policy_docs/{path.name}",
                "chunks": chunks,
            }
        )
    return documents


def _first_h1(text: str) -> str | None:
    """取文档的一级标题作为文档标题。"""
    for raw_line in text.splitlines():
        match = HEADING_RE.match(raw_line.strip())
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return None


async def ingest_documents(
    documents: list[dict], embedder_impl: Embedder | None = None
) -> dict:
    """向量化并写入 pgvector。按 source_path 幂等：重灌前先删除旧切片。"""
    emb = embedder_impl or embedder
    total_chunks = 0
    per_doc: list[tuple[str, int]] = []

    async with AsyncSessionLocal() as session:
        for doc in documents:
            source_path = doc["source_path"]
            chunks: list[tuple[str, str]] = doc["chunks"]
            await session.execute(
                delete(PolicyChunk).where(PolicyChunk.source_path == source_path)
            )
            if not chunks:
                per_doc.append((source_path, 0))
                continue
            vectors = await emb.embed_texts([content for _, content in chunks])
            session.add_all(
                [
                    PolicyChunk(
                        doc_title=doc["doc_title"],
                        section=section,
                        source_path=source_path,
                        chunk_index=index,
                        content=content,
                        embedding=vector,
                    )
                    for index, ((section, content), vector) in enumerate(
                        zip(chunks, vectors)
                    )
                ]
            )
            total_chunks += len(chunks)
            per_doc.append((source_path, len(chunks)))
        await session.commit()

    return {
        "documents": per_doc,
        "total_chunks": total_chunks,
        "policy_docs_dir": str(BASE_DIR / "data" / "policy_docs"),
    }
