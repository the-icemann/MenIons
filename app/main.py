"""The FastAPI application."""

import logging

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import privacy, receipts, webhooks
from app.config import get_settings
from app.db import get_session
from app.payments import callbacks as payment_callbacks

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()  # raises, and so refuses to start, on an unsafe configuration
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    production = settings.env == "production"
    app = FastAPI(
        title="Men-Ions",
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.include_router(webhooks.router)
    app.include_router(receipts.router)
    app.include_router(payment_callbacks.router)
    app.include_router(privacy.router)

    @app.get("/healthz")
    def healthz(session: Session = Depends(get_session)):
        try:
            session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            log.exception("health check: database unreachable")
            return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    return app


app = create_app()
