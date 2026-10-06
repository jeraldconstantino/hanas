"""Application entry point for the HANAS API."""

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.router import api_router
from app.core.config import settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start background tasks on startup and cancel them on shutdown."""
    tasks: list[asyncio.Task] = []

    if settings.app_env == "prod":
        from app.database.runtime_schema import ensure_runtime_schema

        try:
            await asyncio.to_thread(ensure_runtime_schema)
            logger.info("Production runtime schema validation completed.")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Keep the process alive so /health/ready can report a useful failure
            # category instead of Azure replacing the response with a generic 503.
            logger.error("Production runtime schema validation failed: {}", type(exc).__name__)

    if settings.batch_analysis_enabled:
        from app.services.batch_scheduler import run_batch_analysis_loop
        task = asyncio.create_task(run_batch_analysis_loop())
        tasks.append(task)
        logger.info("Batch analysis scheduler task created.")
    else:
        logger.info("Batch analysis scheduler is disabled (batch_analysis_enabled=false).")

    if settings.overview_summary_enabled:
        from app.services.overview_summarizer import run_overview_summary_loop
        task = asyncio.create_task(run_overview_summary_loop())
        tasks.append(task)
        logger.info("Overview summary scheduler task created.")
    else:
        logger.info("Overview summary scheduler is disabled (overview_summary_enabled=false).")

    if settings.semaphore_api_key:
        from app.services.notification_reconciler import run_notification_status_loop
        task = asyncio.create_task(run_notification_status_loop())
        tasks.append(task)
        logger.info("SMS status reconciler task created.")
    else:
        logger.info("SMS status reconciler is disabled because Semaphore is not configured.")

    yield

    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    fastapi_app = FastAPI(
        title=f"{settings.app_name} API",
        description=settings.app_full_name,
        version=settings.app_version,
        lifespan=_lifespan,
    )
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    fastapi_app.include_router(api_router)
    return fastapi_app


app = create_app()
