"""FastAPI 应用入口：配置校验、中间件、路由、生命周期。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import router as v1_router
from app.core.config import get_settings
from app.core.database import close_engine, init_engine
from app.core.errors import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.redis import close_redis
from app.core.request_context import get_request_id, new_request_id, set_request_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    settings.validate_required()  # 缺失必填配置：启动即失败
    init_engine()
    logger = get_logger("main")
    logger.info(
        "platform starting env=%s db=%s@%s:%s rag_query=%s",
        settings.app_env,
        settings.db_name,
        settings.db_host,
        settings.db_port,
        settings.rag_query_base_url or "unset",
    )
    yield
    await close_engine()
    await close_redis()
    from app.rag.rag_document_client import close_rag_document_client
    from app.rag.rag_import_client import close_rag_import_client
    from app.rag.rag_query_client import close_rag_query_client
    from app.rag.rag_trace_client import close_rag_trace_client

    await close_rag_import_client()
    await close_rag_document_client()
    await close_rag_query_client()
    await close_rag_trace_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title="券商财富业务知识管理平台",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request, call_next):
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["X-Request-Id"] = get_request_id()
        return response

    register_exception_handlers(app)
    app.include_router(v1_router)
    return app


app = create_app()
