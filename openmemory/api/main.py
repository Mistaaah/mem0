import logging
import datetime
import os
import secrets
from typing import Optional

from fastapi import Request, HTTPException, Depends
from fastapi.security import APIKeyHeader
from uuid import uuid4
from app.config import DEFAULT_APP_ID, USER_ID
from app.database import Base, SessionLocal, engine
from app.mcp_server import setup_mcp_server
from app.models import App, User
from app.routers import apps_router, backup_router, config_router, memories_router, stats_router
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_pagination import add_pagination

# Setup logging early so we can use it in verify_admin_api_key
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# This tells the API to look for 'X-API-KEY' in the request headers
api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)

async def verify_admin_api_key(
    request: Request,
    api_key: Optional[str] = Depends(api_key_header),
):
    expected_key = os.getenv("ADMIN_API_KEY")

    if not expected_key:
        logger.warning("ADMIN_API_KEY not set — API is unsecured!")
        return None

    # Accept key from X-API-KEY header OR ?api_key= query param (required for SSE clients)
    resolved_key = api_key or request.query_params.get("api_key")

    if not resolved_key:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Provide key via X-API-KEY header or ?api_key= query parameter.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not secrets.compare_digest(resolved_key, expected_key):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return resolved_key


# APPLY THE GUARD:
# Every single endpoint requires a valid ADMIN_API_KEY header.
app = FastAPI(
    title="OpenMemory API",
    dependencies=[Depends(verify_admin_api_key)]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Define functions FIRST
def create_default_user():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == USER_ID).first()
        if not user:
            user = User(
                id=uuid4(),
                user_id=USER_ID,
                name="Default User",
                created_at=datetime.datetime.now(datetime.UTC)
            )
            db.add(user)
            db.commit()
    finally:
        db.close()

def create_default_app():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == USER_ID).first()
        if not user:
            return

        existing_app = db.query(App).filter(
            App.name == DEFAULT_APP_ID,
            App.owner_id == user.id
        ).first()

        if existing_app:
            return

        app = App(
            id=uuid4(),
            name=DEFAULT_APP_ID,
            owner_id=user.id,
            created_at=datetime.datetime.now(datetime.UTC),
            updated_at=datetime.datetime.now(datetime.UTC),
        )
        db.add(app)
        db.commit()
    finally:
        db.close()

# NOW call them with error handling
try:
    logger.info("Attempting to create database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created successfully")
except Exception as e:
    logger.error(f"Failed to create database tables: {e}", exc_info=True)
    raise

try:
    logger.info("Creating default user...")
    create_default_user()
    logger.info("Default user created/verified")
except Exception as e:
    logger.error(f"Failed to create default user: {e}", exc_info=True)
    raise

try:
    logger.info("Creating default app...")
    create_default_app()
    logger.info("Default app created/verified")
except Exception as e:
    logger.error(f"Failed to create default app: {e}", exc_info=True)
    raise

# Setup MCP server
setup_mcp_server(app)

# Include routers
app.include_router(memories_router)
app.include_router(apps_router)
app.include_router(stats_router)
app.include_router(config_router)
app.include_router(backup_router)

# Add pagination support
add_pagination(app)