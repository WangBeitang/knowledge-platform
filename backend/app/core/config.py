"""应用配置：从环境变量 / .env 读取，启动时校验必填项。"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 平台自身
    app_env: str = "development"
    secret_key: str = Field(default="", description="JWT 签名密钥，必填")
    jwt_expires_seconds: int = 21600
    service_api_key: str = Field(default="", description="外部服务密钥，必填")
    cors_origins: str = "http://localhost:5173"

    # MySQL（平台独立库）
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "knowledge_platform"

    # Redis
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # 原 RAG
    rag_query_base_url: str = ""
    rag_import_base_url: str = ""
    rag_service_user_admin: str = "svc_knowledge_admin"
    rag_service_user_employee: str = "svc_knowledge_employee"
    rag_service_user_external: str = "svc_knowledge_external"

    # 三档 Dataset（实际 ID 由 bootstrap 阶段在 RAG 侧确认）
    rag_external_dataset_id: str = "securities_external_public"
    rag_internal_dataset_id: str = "securities_internal_shared"
    rag_admin_dataset_id: str = "securities_admin_private"

    # 初始管理员（幂等初始化使用）
    init_admin_username: str = "admin"
    init_admin_password: str = ""

    @property
    def db_url(self) -> str:
        """SQLAlchemy async MySQL 连接串（用户名/密码做 URL 编码，密码可含 @# 等特殊字符）。"""
        return (
            f"mysql+aiomysql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def validate_required(self) -> None:
        """启动校验：缺失必填配置时明确失败，不静默降级。"""
        missing: list[str] = []
        if not self.secret_key:
            missing.append("SECRET_KEY")
        if not self.service_api_key:
            missing.append("SERVICE_API_KEY")
        if not self.db_name:
            missing.append("DB_NAME")
        if self.db_host == "127.0.0.1" and self.app_env == "production":
            missing.append("DB_HOST（生产环境禁止默认回环地址）")
        if missing:
            raise RuntimeError(f"缺少必需配置项: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
