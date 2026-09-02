from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware

from sentinel_loader.routes import router

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="SentinelLoader",
        description="Parse bulk log files and ingest them into Microsoft Sentinel.",
        version="1.0.0",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key="sentinel-loader-local-session",
        same_site="lax",
        max_age=60 * 60 * 12,
    )
    app.include_router(router)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/styles.css")
    def styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "styles.css", headers={"Cache-Control": "no-store"})

    @app.get("/app.js")
    def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
