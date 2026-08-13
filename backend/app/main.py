"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import conversations, ingest, metrics
from app.config import get_settings
from app.sdk.logging import start_replay_worker

logging.basicConfig(level=logging.INFO)


def run_migrations() -> None:
    """Bring the database to the latest revision.

    Alembic rather than `create_all()`: create_all only ever creates missing
    tables, so it silently ignores every change to an existing one. Adding a
    column or relaxing a constraint left older databases stale, which had
    already forced one ad-hoc `ALTER` at startup.

    Running migrations in-process on boot suits a single-service deployment.
    With multiple replicas this becomes a race, and migrations belong in an
    init container or a deploy step instead — see k8s/README.md.
    """
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(config, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()

    # Replay runs only in the background thread. Draining synchronously here
    # would deadlock: the spool posts to this app's own /ingest endpoint, which
    # cannot answer until startup completes.
    start_replay_worker(get_settings())

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
