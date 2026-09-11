"""FastAPI application entrypoint: `uvicorn app.main:app --reload`."""

from fastapi import FastAPI

from app import __version__
from app.api import review_page
from app.api.v1 import health
from app.api.v1.router import api_router
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
    )
    app.include_router(api_router)
    # Also served unversioned so container and load-balancer health checks do
    # not have to track the API version.
    app.include_router(health.router, include_in_schema=False)
    # The reviewer's page. Server-rendered Jinja2, outside the JSON API prefix.
    app.include_router(review_page.router)
    return app


app = create_app()
