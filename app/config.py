"""应用配置：从 .env 读取，集中管理所有可配置项。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# agent-service 根目录（本文件位于 app/config.py，故上溯两级）
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """全局配置。字段名小写，对应 .env 中的大写环境变量。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Agent 服务自身
    agent_port: int = 8000

    # LLM / Embedding（OpenAI 兼容）
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024

    # Agent 侧 PostgreSQL（只放向量 + 运行日志）
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/blood_agent"

    # 现有 Express 系统
    biz_api_base: str = "http://localhost:3100"
    jwt_secret: str = ""
    agent_service_token: str = ""

    # 运行开关
    # 默认 False（fail-safe）：clone 后忘配 .env 时直连真实接口并报错，
    # 而不是静默用假数据跑通，避免演示/答辩时误用 mock 而不自知
    use_mock_biz: bool = False
    # 临时开关：Embedding 配额暂不可用时，用确定性假向量打通链路（非真实语义向量）
    use_fake_embedding: bool = False
    retrieval_score_threshold: float = 0.35

    # 模型单价（元 / 百万 token，用于成本核算；glm-4-flash 免费，默认 0）
    llm_input_price_per_million: float = 0.0
    llm_output_price_per_million: float = 0.0


# 全局单例
settings = Settings()
