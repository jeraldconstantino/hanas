"""Top-level API router composition."""

from fastapi import APIRouter

from app.api.routes import (
    batch,
    control_cycles,
    health,
    notification_logs,
    overview_summary,
    pipeline,
    sensors,
    settings,
    system_logs,
)

api_router = APIRouter()
api_router.include_router(health.router)

routers = [
    sensors,
    control_cycles,
    system_logs,
    notification_logs,
    settings,
    pipeline,
    batch,
    overview_summary,
]

for i in routers:
    api_router.include_router(i.router, prefix="/api")
