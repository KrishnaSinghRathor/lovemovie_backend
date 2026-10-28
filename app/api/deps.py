import hmac, time
from fastapi import Depends, HTTPException, Request


async def resolve_session(app, sid: str):
    s = app.state.sessions.get(sid)           # in-memory overrides (tests / cache)
    if s:
        return s
    if not sid:
        return None
    doc = await app.state.db.sessions.find_one({"_id": sid})
    if not doc:
        return None
    if doc["expires"] < time.time():
        await app.state.db.sessions.delete_one({"_id": sid})
        return None
    return {"user_id": doc["user_id"], "username": doc["username"], "role": doc["role"], "csrf": doc["csrf"], "sid": sid}


async def current_user(request: Request):
    user = await resolve_session(request.app, request.cookies.get("sid", ""))
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


async def csrf_user(request: Request, user=Depends(current_user)):
    sent = request.headers.get("x-csrf-token", "")
    if not user.get("csrf") or not hmac.compare_digest(sent, user["csrf"]):
        raise HTTPException(403, "CSRF token missing or invalid")
    return user


async def admin_user(user=Depends(csrf_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return user
