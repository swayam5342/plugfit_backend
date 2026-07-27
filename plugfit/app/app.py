from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .db.db import init_db
from .routes.auth import router as authrouter
from .routes.server import router as serverrouter
from .routes.mcp import router as mcprouter
from plugfit.app.logging import setup_logging
from starlette.requests import Request
import logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()

    logger.info("Starting application")

    await init_db()

    yield

    logger.info("Shutting down application")


app = FastAPI(
    title="PlugFit Platform",
    description="Multi-tenant MCP server quality layer. Upload any API spec, get back a cleaned, scored, agent-ready MCP endpoint.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_request(request: Request, call_next):
    # for frontend debug to be removed
    logger.debug(
        "%s %s content-type=%s",
        request.method,
        request.url.path,
        request.headers.get("content-type"),
    )
    response = await call_next(request)
    return response


app.include_router(authrouter, prefix="/auth")
app.include_router(serverrouter)
app.include_router(mcprouter)


@app.get("/", tags=["health"])
async def root():
    logger.info("Health check: root endpoint called")
    return {
        "service": "PlugFit Platform",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health", tags=["health"])
async def health():
    logger.info("Health check: /health endpoint called")
    return {"status": "ok"}
