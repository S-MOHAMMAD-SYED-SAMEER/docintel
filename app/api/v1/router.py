"""Router for /api/v1. Feature routers are mounted here as milestones land."""

from fastapi import APIRouter

from app.api.v1 import documents, health, review

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(documents.router)
api_router.include_router(review.router)
