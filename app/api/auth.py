import re, time, uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from ..core.security import hash_password, token, verify_password
from .deps import csrf_user, current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class Register(BaseModel):
    username: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    password: str = Field(min_length=10, max_length=128)


class Login(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=128)


def _public(u): return {"id": u["_id"], "username": u["username"], "role": u["role"]}


@router.post("/register", status_code=201)
async def register(body: Register, request: Request):
    if not USERNAME.match(body.username):
        raise HTTPException(422, "Username must be 3-32 chars: letters, digits, _ . -")
    db = request.app.state.db
    if await db.users.find_one({"$or": [{"username": body.username}, {"email": body.email.lower()}]}):
        raise HTTPException(409, "Username or email already in use")
    user = {"_id": uuid.uuid4().hex, "username": body.username, "email": body.email.lower(),
            "password_hash": hash_password(body.password), "role": "user", "created_at": time.time()}
    await db.users.insert_one(user)       # role is always "user"; admins are created via the CLI script
    return _public(user)


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    app = request.app
    s = app.state.settings
    key = f"{request.client.host if request.client else '?'}:{body.username.lower()}"
    now = time.time()
    hits = [t for t in app.state.login_attempts.get(key, []) if now - t < s.login_window]
    if len(hits) >= s.login_max_attempts:
        raise HTTPException(429, "Too many login attempts; try again later")
    user = await app.state.db.users.find_one({"username": body.username})
    ok = verify_password(body.password, user["password_hash"] if user else "scrypt$00$00")
    if not (user and ok):
        app.state.login_attempts[key] = hits + [now]
        raise HTTPException(401, "Invalid credentials")
    app.state.login_attempts.pop(key, None)
    sid, csrf = token(), token()
    await app.state.db.sessions.insert_one({"_id": sid, "user_id": user["_id"], "username": user["username"],
                                            "role": user["role"], "csrf": csrf, "expires": now + s.session_ttl})
    response.set_cookie("sid", sid, httponly=True, secure=s.cookie_secure, samesite=s.cookie_samesite, max_age=s.session_ttl, path="/")
    return {"user": _public(user), "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response, user=Depends(csrf_user)):
    await request.app.state.db.sessions.delete_one({"_id": user["sid"]})
    response.delete_cookie("sid", path="/")
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(current_user)):
    return {"user": {"id": user["user_id"], "username": user["username"], "role": user.get("role", "user")}, "csrf_token": user.get("csrf")}
