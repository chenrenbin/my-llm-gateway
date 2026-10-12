from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.routes import router
from .application.gateway import GatewayService
from .application.maintenance import RetentionWorker
from .application.rate_limit import InMemoryRateLimiter
from .application.router import ModelRouter
from .domain.config import GatewayConfig
from .domain.errors import GatewayError, error_payload
from .infrastructure.config import load_config
from .infrastructure.providers.upstream import HttpxUpstreamClient
from .infrastructure.tracing.usage_repo import SqliteUsageRepository
from .observability import logging_setup
from .prompts.repository import PromptRepository

try:  # pragma: no cover - version is optional at runtime
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("llm-gateway")
except Exception:  # pragma: no cover
    __version__ = "0.1.0"


def create_app(
    config: GatewayConfig | None = None,
    *,
    http_client=None,
) -> FastAPI:
    """Assemble the FastAPI app. Injected `config`/`http_client` keep it testable."""
    gateway_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database_path = Path(gateway_config.database_url)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        prompts = PromptRepository(str(database_path))
        usage = SqliteUsageRepository(str(database_path), gateway_config)
        await prompts.initialize()
        await usage.initialize()
        model_router = ModelRouter(gateway_config)
        upstream = HttpxUpstreamClient(gateway_config, http_client)

        app.state.config = gateway_config
        app.state.prompts = prompts
        app.state.usage = usage
        app.state.model_router = model_router
        app.state.upstream = upstream
        app.state.rate_limiter = InMemoryRateLimiter(gateway_config.rate_limit)
        app.state.gateway = GatewayService(
            gateway_config, model_router, upstream, prompts, usage
        )
        app.state.retention = RetentionWorker(usage, gateway_config)
        app.state.retention.start()
        yield
        await app.state.retention.stop()
        await upstream.close()

    app = FastAPI(
        title="Unified LLM Gateway",
        version=__version__,
        description="OpenAI-compatible multi-model gateway for Agent Engineering Platforms.",
        lifespan=lifespan,
    )
    app.add_exception_handler(GatewayError, _gateway_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.include_router(router)
    return app


async def _gateway_error_handler(_: Request, error: GatewayError) -> JSONResponse:
    headers = {"Retry-After": "1"} if error.status_code == 429 else None
    return JSONResponse(error_payload(error), status_code=error.status_code, headers=headers)


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "Invalid request",
                "type": "invalid_request_error",
                "param": None,
                "code": "validation_error",
                "details": exc.errors(),
            }
        },
        status_code=422,
    )


def build_app(
    config: GatewayConfig | None = None,
    http_client=None,
) -> FastAPI:
    """Production factory: load config (fail-fast) and mount routes.

    Run with: `uvicorn gateway.main:build_app --factory`
    """
    gateway_config = config or load_config()
    logging_setup.init()
    return create_app(gateway_config, http_client=http_client)
