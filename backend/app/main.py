import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.routers.chat import router as chat_router
from app.services.llm import LLMService
from app.services.retrieval import RetrievalService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        settings = get_settings()
        app.state.settings = settings

    if (
        settings.applicationinsights_connection_string
        and not getattr(app.state, "telemetry_configured", False)
    ):
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(connection_string=settings.applicationinsights_connection_string)
            app.state.telemetry_configured = True
        except Exception as exc:
            logger.warning("Failed to configure Azure Monitor: %s", exc)

    if not hasattr(app.state, "retrieval_service"):
        app.state.retrieval_service = RetrievalService(settings=settings)

    if not hasattr(app.state, "llm_service"):
        app.state.llm_service = LLMService(settings=settings)

    try:
        yield
    finally:
        for service_name in ("llm_service", "retrieval_service"):
            service = getattr(app.state, service_name, None)
            close = getattr(service, "close", None)
            if close is not None:
                await close()


def _resolve_frontend_origin() -> str:
    try:
        return get_settings().frontend_origin
    except ValidationError:
        return "http://localhost:3000"


def create_app() -> FastAPI:
    app = FastAPI(
        title="NAV RAG Chatbot Backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    origin = _resolve_frontend_origin()
    origins = [o.strip() for o in origin.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(chat_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
