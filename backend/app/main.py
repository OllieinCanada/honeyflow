import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import check_connection, close_engine
from app.models.attribution import (  # noqa: F401 - register Alembic metadata
    AttributionManifestRecord,
    AttributionReviewOverlayRecord,
)
from app.models.badge import Badge  # noqa: F401 – ensure table is registered
from app.routes import (
    attribution,
    citation_graph,
    contributions,
    donations,
    graph,
    jury,
    package_graph,
    projects,
    stream,
    users,
    vault,
)
from app.services.donation_db import init_donations_db
from app.services.vault_db import init_db
from app.services.withdrawal_db import init_withdrawals_db


def _setup_logging() -> None:
    fmt = "%(asctime)s  %(levelname)-5s  [%(name)s]  %(message)s"
    logging.basicConfig(format=fmt, datefmt="%H:%M:%S", level=logging.INFO)
    logging.getLogger("app").setLevel(logging.DEBUG if settings.debug else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize database connectivity on startup and close on shutdown."""
    log = logging.getLogger(__name__)
    log.info("Checking database connection...")
    await check_connection()
    log.info("Database connection OK.")
    await init_db()
    log.info("Vault table ready.")
    await init_donations_db()
    log.info("Donations table ready.")
    await init_withdrawals_db()
    log.info("Withdrawals table ready.")
    yield
    from app.services.screenshot import close_browser

    log.info("Closing Playwright browser...")
    await close_browser()
    log.info("Disposing database engine...")
    await close_engine()
    log.info("Database engine disposed.")


def create_app() -> FastAPI:
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("GitHub token: %s", "configured" if settings.github_token else "NOT SET")
    log.info("0G inference API: %s", settings.inference_api_url)

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(contributions.router)
    app.include_router(attribution.router)
    app.include_router(donations.router)
    app.include_router(graph.router)
    app.include_router(citation_graph.router)
    app.include_router(package_graph.router)
    app.include_router(projects.router)
    app.include_router(jury.router)
    app.include_router(stream.router)
    app.include_router(users.router)
    app.include_router(vault.router)

    # Keep static mounted for existing assets/backward compatibility.
    static_dir = Path(__file__).resolve().parent.parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
