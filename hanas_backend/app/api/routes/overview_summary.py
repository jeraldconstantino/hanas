"""Overview daily summary routes."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import verify_operator_token
from app.core.config import settings
from app.schemas.sensor import OverviewSummaryStatus
from app.services.overview_summarizer import (
    get_latest_overview_summary,
    get_overview_summary_scheduler_state,
    run_overview_summary_cycle,
)

router = APIRouter(prefix="/overview-summary", tags=["overview-summary"])


@router.get("", response_model=OverviewSummaryStatus)
def get_overview_summary() -> OverviewSummaryStatus:
    """Return the latest 12-hour scheduled operator summary and scheduler state."""
    return OverviewSummaryStatus(
        **get_overview_summary_scheduler_state(),
        latest_summary=get_latest_overview_summary(),
    )


@router.post("/trigger", response_model=OverviewSummaryStatus)
async def trigger_overview_summary(
    _: None = Depends(verify_operator_token),
) -> OverviewSummaryStatus:
    """Manually regenerate the Overview summary."""
    summary = await asyncio.to_thread(
        run_overview_summary_cycle,
        trigger_source="manual_summary_trigger",
    )
    if summary is None and settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM overview summary was not generated. Check OpenAI configuration, readings, and backend logs.",
        )
    return OverviewSummaryStatus(
        **get_overview_summary_scheduler_state(),
        latest_summary=get_latest_overview_summary(),
    )
