import time, uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from ..storage.local import BadMedia, TooLarge
from .deps import admin_user, current_user

router = APIRouter(prefix="/api/movies", tags=["movies"])
MEDIA = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/mkv" }


def _public(m):
    return {k: m.get(k) for k in ("title", "description", "file_size", "duration", "resolution", "checksum", "created_at")} | {"id": m["_id"]}


def _can_download(app, movie_id: str, user: dict, need_download: bool = True) -> bool:
    if user.get("role") == "admin":
        return True
    return any(r.state.movie_id == movie_id and user["user_id"] in r.allowed and r.status == "open"
               and (not need_download or getattr(r, "allow_downloads", True)) for r in app.state.rooms.values())


@router.post("", status_code=201)
async def upload(request: Request, title: str = Query(min_length=1, max_length=200),
                 filename: str = Query(max_length=255), duration: float = Query(gt=0, le=86400),
                 description: str = Query("", max_length=2000), resolution: str | None = Query(None, max_length=20),
                 user=Depends(admin_user)):
    """Raw-body streaming upload (body = file bytes) so the server never buffers the movie."""
    s, ext = request.app.state.settings, Path(filename).suffix.lower()
    if ext not in MEDIA:
        raise HTTPException(415, f"Supported formats: {', '.join(MEDIA)}")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > s.max_upload_bytes:
        raise HTTPException(413, "File too large")
    try:
        key, size, sha = await request.app.state.storage.save(request.stream(), s.max_upload_bytes, ext)
    except TooLarge:
        raise HTTPException(413, "File too large")
    except BadMedia:
        raise HTTPException(415, "File content does not match its type")
    movie = {"_id": uuid.uuid4().hex, "title": title, "description": description, "storage_key": key, "file_size": size,
             "duration": duration, "resolution": resolution, "checksum": sha, "uploaded_by": user["user_id"], "created_at": time.time()}
    await request.app.state.db.movies.insert_one(movie)
    return _public(movie)


@router.get("")
async def list_movies(request: Request, user=Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return [_public(m) async for m in request.app.state.db.movies.find({}).sort("created_at", -1)]


@router.get("/{movie_id}")
async def get_movie(movie_id: str, request: Request, user=Depends(current_user)):
    m = await request.app.state.db.movies.find_one({"_id": movie_id})
    if not m or not _can_download(request.app, movie_id, user):
        raise HTTPException(404, "Movie not found")
    return _public(m)


@router.delete("/{movie_id}")
async def delete_movie(movie_id: str, request: Request, user=Depends(admin_user)):
    m = await request.app.state.db.movies.find_one({"_id": movie_id})
    if not m:
        raise HTTPException(404, "Movie not found")
    if any(r.state.movie_id == movie_id and r.status == "open" for r in request.app.state.rooms.values()):
        raise HTTPException(409, "Movie is in use by an open room")
    await request.app.state.db.movies.delete_one({"_id": movie_id})
    await request.app.state.db.subtitles.delete_many({"movie_id": movie_id})
    request.app.state.storage.delete(m["storage_key"])
    return {"ok": True}


@router.get("/{movie_id}/download")
async def download(movie_id: str, request: Request, user=Depends(current_user)):
    m = await request.app.state.db.movies.find_one({"_id": movie_id})
    if not m or not _can_download(request.app, movie_id, user):
        raise HTTPException(404, "Movie not found")   # 404 for both, so ids can't be probed
    p = request.app.state.storage.path(m["storage_key"])
    return FileResponse(p, media_type=MEDIA[p.suffix], headers={"X-Content-SHA256": m["checksum"], "Cache-Control": "private, no-store"})
