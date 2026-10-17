from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    chat,
    dashboard,
    documents,
    health,
    organizations,
    search,
    workspaces,
)

api_router = APIRouter()
for module in (
    health,
    auth,
    organizations,
    workspaces,
    documents,
    search,
    chat,
    dashboard,
    admin,
):
    api_router.include_router(module.router)
