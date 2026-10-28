import hashlib, os, time
import pytest
from mongomock_motor import AsyncMongoMockClient
from starlette.testclient import TestClient
from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from app.websocket.hub import Room
from app.sync.engine import PlaybackState

MP4 = b"\x00\x00\x00\x18ftypmp42" + os.urandom(4000)
PW = "correct-horse-battery"


@pytest.fixture
def env(tmp_path):
    db = AsyncMongoMockClient()["t"]
    app = create_app(db=db, settings=Settings(storage_dir=str(tmp_path / "m"), max_upload_bytes=10_000, login_max_attempts=3))
    with TestClient(app) as c:
        c.portal.call(db.users.insert_one, {"_id": "adm", "username": "boss", "email": "b@x.io", "password_hash": hash_password(PW), "role": "admin"})
        yield c, app, tmp_path


def login(c, name, pw=PW):
    r = c.post("/api/auth/login", json={"username": name, "password": pw})
    return r, {"X-CSRF-Token": r.json().get("csrf_token", "")}


def register(c, name):
    assert c.post("/api/auth/register", json={"username": name, "email": f"{name}@x.io", "password": PW}).status_code == 201


def upload(c, h, data=MP4, name="film.mp4", dur=120):
    return c.post(f"/api/movies?title=Film&filename={name}&duration={dur}", content=data, headers=h)


def test_register_login_cookie_flags_and_roles(env):
    c, *_ = env
    register(c, "alice")
    assert c.post("/api/auth/register", json={"username": "alice", "email": "z@x.io", "password": PW}).status_code == 409
    r, h = login(c, "alice")
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/me").json()["user"]["role"] == "user"
    assert c.post("/api/auth/logout").status_code == 403           # no CSRF header
    assert c.post("/api/auth/logout", headers=h).status_code == 200
    assert c.get("/api/auth/me").status_code == 401


def test_login_rate_limit(env):
    c, *_ = env
    codes = [login(c, "boss", "wrong-password")[0].status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]


def test_upload_requires_admin_and_csrf(env):
    c, *_ = env
    assert upload(c, {}).status_code == 401
    register(c, "bob"); _, h = login(c, "bob")
    assert upload(c, h).status_code == 403
    login(c, "boss")
    assert upload(c, {}).status_code == 403                        # admin session but no CSRF


def test_upload_validation_checksum_and_traversal(env):
    c, app, tmp = env
    _, h = login(c, "boss")
    r = upload(c, h, name="../../evil.mp4")
    assert r.status_code == 201 and r.json()["checksum"] == hashlib.sha256(MP4).hexdigest()
    assert "storage_key" not in r.json()
    assert all(p.parent == tmp / "m" for p in (tmp / "m").iterdir())
    assert upload(c, h, name="x.exe").status_code == 415
    assert upload(c, h, data=b"not a video at all, sorry").status_code == 415
    assert upload(c, h, data=b"\x00\x00\x00\x18ftyp" + b"0" * 20000).status_code == 413
    assert upload(c, h, dur=0).status_code == 422
    assert not list((tmp / "m").glob("*.part"))                    # failed uploads leave nothing behind
    assert len(c.get("/api/movies").json()) == 1


def test_download_authorization_and_range(env):
    c, app, _ = env
    _, h = login(c, "boss")
    mid = upload(c, h).json()["id"]
    room = Room(PlaybackState("r1", mid, 120.0), "adm", "x", time.time)
    app.state.rooms["r1"] = room
    register(c, "carol"); register(c, "dave")
    c.cookies.clear()
    assert c.get(f"/api/movies/{mid}/download").status_code == 401
    login(c, "carol")
    assert c.get(f"/api/movies/{mid}/download").status_code == 404           # not a room member
    room.allowed.add(c.portal.call(app.state.db.users.find_one, {"username": "carol"})["_id"])
    r = c.get(f"/api/movies/{mid}/download")
    assert r.status_code == 200 and r.content == MP4 and r.headers["x-content-sha256"] == hashlib.sha256(MP4).hexdigest()
    r = c.get(f"/api/movies/{mid}/download", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == MP4[:10]
    room.allow_downloads = False
    assert c.get(f"/api/movies/{mid}/download").status_code == 404
    room.allow_downloads = True
    c.cookies.clear(); login(c, "dave")
    assert c.get(f"/api/movies/{mid}/download").status_code == 404


def test_delete_removes_file_and_blocks_when_in_use(env):
    c, app, tmp = env
    _, h = login(c, "boss")
    mid = upload(c, h).json()["id"]
    app.state.rooms["r"] = Room(PlaybackState("r", mid, 1.0), "adm", "x", time.time)
    assert c.delete(f"/api/movies/{mid}", headers=h).status_code == 409
    app.state.rooms["r"].status = "ended"
    assert c.delete(f"/api/movies/{mid}", headers=h).status_code == 200
    assert not list((tmp / "m").iterdir())


def test_ws_accepts_db_backed_session(env):
    c, app, _ = env
    register(c, "erin"); login(c, "erin")
    room = Room(PlaybackState("r9", "m", 10.0), "adm", "x", time.time)
    uid = c.portal.call(app.state.db.users.find_one, {"username": "erin"})["_id"]
    room.allowed.add(uid); app.state.rooms["r9"] = room
    with c.websocket_connect("/ws/rooms/r9") as ws:
        assert ws.receive_json()["role"] == "participant"
