"""Per-movie subtitle tracks (SRT/VTT upload by the admin, stored as WebVTT in MongoDB)."""
import re, time, uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from .deps import admin_user, current_user
from .movies import _can_download

router = APIRouter(tags=["subtitles"])


def to_vtt(text: str) -> str:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("WEBVTT"):
        return text + "\n"
    return "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text) + "\n"


class NewSub(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    lang: str = Field("", max_length=12, pattern=r"^[A-Za-z0-9-]*$")
    content: str = Field(max_length=1_000_000)


def _visible(request: Request, movie_id: str, user: dict):
    if not _can_download(request.app, movie_id, user, need_download=False):
        raise HTTPException(404, "Movie not found")


@router.post("/api/movies/{movie_id}/subtitles", status_code=201)
async def add(movie_id: str, body: NewSub, request: Request, user=Depends(admin_user)):
    db = request.app.state.db
    if not await db.movies.find_one({"_id": movie_id}):
        raise HTTPException(404, "Movie not found")
    if await db.subtitles.count_documents({"movie_id": movie_id}) >= 20:
        raise HTTPException(409, "At most 20 subtitle tracks per movie")
    vtt = to_vtt(body.content)
    if "-->" not in vtt:
        raise HTTPException(422, "No subtitle cues found; upload a .srt or .vtt file")
    doc = {"_id": uuid.uuid4().hex, "movie_id": movie_id, "label": body.label.strip(), "lang": body.lang, "vtt": vtt, "created_at": time.time()}
    await db.subtitles.insert_one(doc)
    return {"id": doc["_id"], "label": doc["label"], "lang": doc["lang"]}


@router.get("/api/movies/{movie_id}/subtitles")
async def list_subs(movie_id: str, request: Request, user=Depends(current_user)):
    _visible(request, movie_id, user)
    return [{"id": d["_id"], "label": d["label"], "lang": d["lang"]} async for d in request.app.state.db.subtitles.find({"movie_id": movie_id}).sort("created_at", 1)]


@router.get("/api/subtitles/{sid}")
async def get_sub(sid: str, request: Request, user=Depends(current_user)):
    d = await request.app.state.db.subtitles.find_one({"_id": sid})
    if not d:
        raise HTTPException(404, "Subtitle not found")
    _visible(request, d["movie_id"], user)
    return Response(d["vtt"], media_type="text/vtt; charset=utf-8", headers={"Cache-Control": "private, max-age=300"})


@router.delete("/api/subtitles/{sid}")
async def delete_sub(sid: str, request: Request, user=Depends(admin_user)):
    await request.app.state.db.subtitles.delete_one({"_id": sid})
    return {"ok": True}
