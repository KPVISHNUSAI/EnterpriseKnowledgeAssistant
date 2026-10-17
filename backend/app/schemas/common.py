from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str
    details: Any | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


T = TypeVar("T")


class Page(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic model
    items: list[T]
    total: int
    page: int
    page_size: int


class PageParams:
    """Common `?page=&page_size=` query parameters (page_size capped at 100)."""

    def __init__(
        self,
        page: int = Query(1, ge=1, description="1-based page number"),
        page_size: int = Query(20, ge=1, le=100),
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class UserRef(Schema):
    id: uuid.UUID
    email: str
    full_name: str


# Reusable OpenAPI `responses=` declarations.
def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    descriptions = {
        400: "Bad request",
        401: "Not authenticated",
        403: "Forbidden (missing permission or CSRF token)",
        404: "Not found (or not visible to the caller)",
        409: "Conflict",
        413: "Payload too large",
        415: "Unsupported file type",
        422: "Validation error",
        429: "Rate limited",
        503: "Service unavailable",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}
