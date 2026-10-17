from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal
from app.schemas.common import errors
from app.schemas.dashboard import DashboardResponse
from app.services import dashboard as dashboard_service
from app.services.access import Principal

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse, responses=errors(401))
def get_dashboard(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> DashboardResponse:
    """Document counts by ingestion status, recent documents and conversations, and
    recent activity - all limited to what the caller can see."""
    return dashboard_service.get_dashboard(db, principal)
