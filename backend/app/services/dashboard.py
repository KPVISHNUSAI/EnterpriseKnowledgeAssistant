"""Home dashboard summary, limited to what the caller can see."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.schemas.dashboard import DashboardResponse
from app.services.access import Principal


def get_dashboard(db: Session, principal: Principal) -> DashboardResponse:
    raise NotImplementedError
