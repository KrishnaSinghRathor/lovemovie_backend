import secrets, time, uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from ..core.security import hash_password, verify_password
from ..sync.engine import PlaybackState
from ..websocket.hub import Room
from .deps import admin_user, csrf_user, current_user

router = APIRouter(prefix="/api/rooms", tags=["rooms"])
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no look-alike characters


class Create(BaseModel):
    movie_id: str
    password: str | None = Field(None, min_length=4, max_length=128)
    allow_downloads: bool = True


class Patch(BaseModel):
    locked: bool | None = None
    allow_downloads: bool | None = None


class Join(BaseModel):
    room_code: str = Field(max_length=16)
    password: str | None = Field(None, max_length=128)


class Kick(BaseModel):
    user_id: str


def build_room(app, doc: dict, movie: dict, admin_name: str) -> Room:
    r = Room(PlaybackState(doc["_id"], movie["_id"], movie["duration"]), doc["admin_id"], movie["checksum"], app.state.clock)
    r.room_code, r.password_hash, r.admin_name = doc["room_code"], doc.get("password_hash"), admin_name
    r.locked, r.allow_downloads, r.allowed = doc.get("locked", False), doc.get("allow_downloads", True), {doc["admin_id"], *doc.get("members", [])}
    return r


async def restore_rooms(app):
    """Rebuild open rooms after a restart (playback resets to PAUSED at 0; members keep access)."""
    db = app.state.db
    async for d in db.rooms.find({"status": "open"}):
        m, a = await db.movies.find_one({"_id": d["movie_id"]}), await db.users.find_one({"_id": d["admin_id"]})
        if m and a:
            app.state.rooms[d["_id"]] = build_room(app, d, m, a["username"])


def _info(room: Room, admin_view=False):
    out = {"id": room.state.room_id, "movie_id": room.state.movie_id, "admin": room.admin_name, "status": room.status,
           "locked": room.locked, "allow_downloads": room.allow_downloads, "has_password": bool(room.password_hash)}
    if admin_view:
        out["room_code"] = room.room_code
    return out


def _get(app, rid: str) -> Room:
    r = app.state.rooms.get(rid)
    if not r:
        raise HTTPException(404, "Room not found")
    return r


def _own(app, rid: str, user) -> Room:
    r = _get(app, rid)
    if r.admin_id != user["user_id"]:
        raise HTTPException(404, "Room not found")
    return r


@router.post("", status_code=201)
async def create(body: Create, request: Request, user=Depends(admin_user)):
    app = request.app
    movie = await app.state.db.movies.find_one({"_id": body.movie_id})
    if not movie:
        raise HTTPException(404, "Movie not found")
    doc = {"_id": uuid.uuid4().hex, "room_code": "".join(secrets.choice(ALPHABET) for _ in range(8)), "movie_id": movie["_id"],
           "admin_id": user["user_id"], "password_hash": hash_password(body.password) if body.password else None,
           "allow_downloads": body.allow_downloads, "locked": False, "status": "open", "members": [], "created_at": time.time()}
    await app.state.db.rooms.insert_one(doc)
    room = build_room(app, doc, movie, user["username"])
    app.state.rooms[doc["_id"]] = room
    return _info(room, admin_view=True) | {"invite_path": f"/join/{room.room_code}"}


@router.get("")
async def mine(request: Request, user=Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return [_info(r, True) for r in request.app.state.rooms.values() if r.admin_id == user["user_id"] and r.status == "open"]


@router.post("/join")
async def join(body: Join, request: Request, user=Depends(csrf_user)):
    app, s, uid, now = request.app, request.app.state.settings, user["user_id"], time.time()
    hits = [t for t in app.state.join_attempts.get(uid, []) if now - t < s.login_window]
    if len(hits) >= s.login_max_attempts:
        raise HTTPException(429, "Too many failed attempts; try again later")

    def fail(code, msg):
        app.state.join_attempts[uid] = hits + [now]
        raise HTTPException(code, msg)

    code = body.room_code.strip().upper()
    room = next((r for r in app.state.rooms.values() if r.room_code == code and r.status == "open"), None)
    if not room or uid in room.banned:
        fail(404, "Room not found")
    if uid != room.admin_id:
        if room.locked:
            fail(403, "Room is locked")
        if room.password_hash:
            if not body.password:
                raise HTTPException(403, "Password required")
            if not verify_password(body.password, room.password_hash):
                fail(403, "Incorrect password")
    room.allowed.add(uid)
    await app.state.db.rooms.update_one({"_id": room.state.room_id}, {"$addToSet": {"members": uid}})
    return _info(room)


@router.get("/{room_id}")
async def detail(room_id: str, request: Request, user=Depends(current_user)):
    room = _get(request.app, room_id)
    if user["user_id"] not in room.allowed:
        raise HTTPException(404, "Room not found")
    return _info(room, user["user_id"] == room.admin_id) | {"participants": room._participants(), "state": room._snap()}


@router.post("/{room_id}/leave")
async def leave(room_id: str, request: Request, user=Depends(csrf_user)):
    room = _get(request.app, room_id)
    if user["user_id"] == room.admin_id or user["user_id"] not in room.allowed:
        raise HTTPException(400, "Not a participant")
    await room.kick(user["user_id"])
    await request.app.state.db.rooms.update_one({"_id": room_id}, {"$pull": {"members": user["user_id"]}})
    return {"ok": True}


@router.patch("/{room_id}")
async def update(room_id: str, body: Patch, request: Request, user=Depends(admin_user)):
    room = _own(request.app, room_id, user)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(room, k, v)
    if changes:
        await request.app.state.db.rooms.update_one({"_id": room_id}, {"$set": changes})
    return _info(room, True)


@router.post("/{room_id}/kick")
async def kick(room_id: str, body: Kick, request: Request, user=Depends(admin_user)):
    room = _own(request.app, room_id, user)
    if body.user_id == room.admin_id:
        raise HTTPException(400, "Cannot remove the admin")
    room.banned.add(body.user_id)
    await room.kick(body.user_id)
    await request.app.state.db.rooms.update_one({"_id": room_id}, {"$pull": {"members": body.user_id}})
    return {"ok": True}


@router.post("/{room_id}/end")
async def end(room_id: str, request: Request, user=Depends(admin_user)):
    room = _own(request.app, room_id, user)
    await room.end()
    await request.app.state.db.rooms.update_one({"_id": room_id}, {"$set": {"status": "ended"}})
    return {"ok": True}
