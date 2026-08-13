"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import conversations, ingest, metrics
from app.config import get_settings
from app.db.models import Base
from app.db.session import engine

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 1 uses create_all for speed. A real deployment wants Alembic
    # migrations; noted in the README as a known gap.
    Base.metadata.create_all(bind=engine)

    # create_all() only creates missing tables — it never alters existing ones.
    # Databases created before conversation_id became nullable still carry the
    # NOT NULL constraint, which would block deleting a conversation while
    # keeping its telemetry. Exactly the class of problem Alembic exists to
    # solve; this is a targeted stopgap, not a migration system.
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE inference_logs ALTER COLUMN conversation_id DROP NOT NULL")
        )

    yield


app = FastAPI(title="LLM Inference Logging & Ingestion System", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(conversations.router)
app.include_router(ingest.router)
app.include_router(metrics.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
