"""Embedding 封装：走 OpenAI 兼容协议（阿里云百炼 text-embedding-v3）。

另有临时假向量通道（USE_FAKE_EMBEDDING=true）：Embedding 配额不可用时，
用确定性哈希词袋向量跳过接口，仅用于验证工程链路，不是真实语义向量。
"""
import asyncio
import logging
import math
import zlib

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# 百炼单次请求最多 10 条文本
BATCH_SIZE = 10
# 失败重试次数（指数退避）
MAX_RETRIES = 3

# 假向量需要剔除的标点与空白，避免噪声拉高无关文本的相似度
_NOISE_CHARS = set(
    "，。、；：？！“”‘’（）《》〈〉「」【】…—·,.!?;:\"'()[]{}<>-—/\\|@#$%^&*+=~`_ \t\r\n"
)

_fake_warned = False


def fake_vector(text: str) -> list[float]:
    """确定性假向量：字符 unigram + bigram 哈希词袋，L2 归一化。

    跨进程稳定（用 crc32 而非内置 hash，避免 PYTHONHASHSEED 影响），
    相似度反映的是字面重合度而非语义，仅供链路验证。
    """
    dim = settings.embedding_dim
    compact = "".join(ch for ch in text if ch not in _NOISE_CHARS)
    vector = [0.0] * dim
    for ch in compact:
        vector[zlib.crc32(ch.encode("utf-8")) % dim] += 0.5
    for index in range(len(compact) - 1):
        bigram = compact[index : index + 2]
        vector[zlib.crc32(bigram.encode("utf-8")) % dim] += 1.0

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]



class Embedder:
    """把文本转成向量。批量调用时自动按 BATCH_SIZE 拆分。"""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=60.0,
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与输入等长、顺序一致的向量列表。"""
        if not texts:
            return []
        if settings.use_fake_embedding:
            global _fake_warned
            if not _fake_warned:
                logger.warning(
                    "USE_FAKE_EMBEDDING=true：正在使用确定性假向量，检索结果非真实语义相似度"
                )
                _fake_warned = True
            return [fake_vector(text) for text in texts]

        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        """单条向量化。"""
        vectors = await self.embed_texts([text])
        return vectors[0]

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """单批调用，带重试与指数退避。"""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.embeddings.create(
                    model=settings.embedding_model,
                    input=batch,
                    dimensions=settings.embedding_dim,
                )
                # 服务端可能乱序返回，按 index 排回原顺序
                ordered = sorted(response.data, key=lambda item: item.index)
                return [item.embedding for item in ordered]
            except Exception as exc:  # noqa: BLE001 - 需要兜住 SDK 各类异常后重试
                last_error = exc
                wait = 2**attempt
                logger.warning(
                    "Embedding 第 %d/%d 次失败：%s，%d 秒后重试",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
        raise RuntimeError(f"Embedding 连续 {MAX_RETRIES} 次失败：{last_error}")


# 全局单例（无状态，可安全复用）
embedder = Embedder()
