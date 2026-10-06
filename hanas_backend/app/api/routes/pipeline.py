"""Pipeline progress endpoint for real-time dashboard animation."""

from fastapi import APIRouter

from app.services import pipeline_tracker

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/progress")
def get_pipeline_progress() -> dict:
    """Return the current agentic AI pipeline stage.

    Returns ``{"stage": null}`` when no pipeline is running.
    Returns ``{"stage": "completed"}`` immediately after a cycle finishes.
    Clients should poll at ~1 s intervals while on the pipeline dashboard.
    """
    return pipeline_tracker.get_progress()
