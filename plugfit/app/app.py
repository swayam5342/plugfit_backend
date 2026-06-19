from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .db.db import init_db
from .routes.auth import router as authrouter
from .routes.server import router as serverrouter


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


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


app.include_router(authrouter, prefix="/auth")
app.include_router(serverrouter)


@app.get("/", tags=["health"])
async def root():
    return {
        "service": "PlugFit Platform",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
