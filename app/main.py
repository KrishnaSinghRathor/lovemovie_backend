import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from .api import auth, movies, rooms, subtitles
from .core.config import Settings
from .storage.local import LocalStorage
from .websocket.hub import handle_ws


def create_app(db=None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if db is None:
        from motor.motor_asyncio import AsyncIOMotorClient
        db = AsyncIOMotorClient(settings.mongo_url)[settings.db_name]

    @asynccontextmanager
    async def lifespan(app):
        await db.users.create_index("username", unique=True)
        await db.users.create_index("email", unique=True)
        await db.movies.create_index("created_at")
        await db.rooms.create_index("room_code", unique=True)
        await rooms.restore_rooms(app)
        yield

    app = FastAPI(title="SyncCinema", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=sorted(settings.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token", "Range"])
    s = app.state
    s.settings, s.db, s.storage = settings, db, LocalStorage(settings.storage_dir)
    s.rooms, s.sessions, s.login_attempts, s.join_attempts = {}, {}, {}, {}
    s.allowed_origins, s.clock = settings.allowed_origins, time.time
    app.include_router(auth.router)
    app.include_router(movies.router)
    app.include_router(rooms.router)
    app.include_router(subtitles.router)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.websocket("/ws/rooms/{room_id}")
    async def ws_room(ws: WebSocket, room_id: str):
        await handle_ws(ws, room_id)

    return app


app = create_app()
