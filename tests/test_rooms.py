import pytest
from mongomock_motor import AsyncMongoMockClient
from starlette.testclient import TestClient
from test_auth_movies import MP4, PW, login, register, upload
from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app


def mk(tmp_path, db):
    return create_app(db=db, settings=Settings(storage_dir=str(tmp_path / "m"), login_max_attempts=3))


@pytest.fixture
def env(tmp_path):
    db = AsyncMongoMockClient()["t"]
    app = mk(tmp_path, db)
    with TestClient(app) as c:
        c.portal.call(db.users.insert_one, {"_id": "adm", "username": "boss", "email": "b@x.io", "password_hash": hash_password(PW), "role": "admin"})
        yield c, app, db, tmp_path


def make(c, **kw):
    _, h = login(c, "boss")
    mid = upload(c, h).json()["id"]
    return h, mid, c.post("/api/rooms", json={"movie_id": mid, **kw}, headers=h)


def as_user(c, name):
    c.cookies.clear()
    _, h = login(c, name)
    return h, c.cookies.get("sid")


def ws(c, sid, rid):
    return c.websocket_connect(f"/ws/rooms/{rid}", headers={"cookie": f"sid={sid}"})


def until(w, typ, n=10):
    for _ in range(n):
        m = w.receive_json()
        if m["type"] == typ:
            return m
    raise AssertionError(typ)


def test_create_permissions(env):
    c, *_ = env
    h, mid, r = make(c)
    b = r.json()
    assert r.status_code == 201 and len(b["room_code"]) == 8 and "password" not in str(b).lower().replace("has_password", "")
    assert c.post("/api/rooms", json={"movie_id": "nope"}, headers=h).status_code == 404
    register(c, "alice"); h2, _ = as_user(c, "alice")
    assert c.post("/api/rooms", json={"movie_id": mid}, headers=h2).status_code == 403
    assert c.get("/api/rooms").status_code == 403


def test_join_password_lock_downloads_ws(env):
    c, *_ = env
    h, mid, r = make(c, password="movie-night")
    code, rid = r.json()["room_code"], r.json()["id"]
    register(c, "alice"); register(c, "bob")
    h2, sid = as_user(c, "alice")
    join = lambda body, hh: c.post("/api/rooms/join", json=body, headers=hh)
    assert join({"room_code": code}, h2).status_code == 403
    assert join({"room_code": code, "password": "wrong"}, h2).status_code == 403
    assert join({"room_code": code.lower(), "password": "movie-night"}, h2).json()["id"] == rid
    assert c.get(f"/api/movies/{mid}/download").content == MP4
    with ws(c, sid, rid) as w:
        assert until(w, "ROOM_STATE")["role"] == "participant"
    assert c.get(f"/api/rooms/{rid}").json()["participants"] == []
    h, _ = as_user(c, "boss")
    assert c.patch(f"/api/rooms/{rid}", json={"locked": True}, headers=h).json()["locked"] is True
    hb, _ = as_user(c, "bob")
    assert join({"room_code": code, "password": "movie-night"}, hb).status_code == 403    # locked
    as_user(c, "alice")
    assert c.get(f"/api/movies/{mid}/download").status_code == 200                          # existing member unaffected
    h, _ = as_user(c, "boss")
    assert c.patch(f"/api/rooms/{rid}", json={"allow_downloads": False}, headers=h).status_code == 200
    as_user(c, "alice")
    assert c.get(f"/api/movies/{mid}/download").status_code == 404


def test_kick_and_end(env):
    c, *_ = env
    h, mid, r = make(c)
    code, rid = r.json()["room_code"], r.json()["id"]
    register(c, "alice"); h2, sid = as_user(c, "alice")
    c.post("/api/rooms/join", json={"room_code": code}, headers=h2)
    uid = c.get("/api/auth/me").json()["user"]["id"]
    h, _ = as_user(c, "boss")
    with ws(c, sid, rid) as w:
        until(w, "ROOM_STATE")
        assert c.post(f"/api/rooms/{rid}/kick", json={"user_id": uid}, headers=h).status_code == 200
        assert until(w, "ERROR")["code"] == "REMOVED"
    h2, sid = as_user(c, "alice")
    assert c.post("/api/rooms/join", json={"room_code": code}, headers=h2).status_code == 404   # banned
    assert c.get(f"/api/movies/{mid}/download").status_code == 404
    h, bsid = as_user(c, "boss")
    with ws(c, bsid, rid) as w:
        until(w, "ROOM_STATE")
        assert c.post(f"/api/rooms/{rid}/end", headers=h).status_code == 200
        until(w, "ROOM_ENDED")
    assert c.post("/api/rooms/join", json={"room_code": code}, headers=h).status_code == 404


def test_participant_cannot_use_admin_endpoints(env):
    c, *_ = env
    _, mid, r = make(c)
    rid, code = r.json()["id"], r.json()["room_code"]
    register(c, "alice"); h2, _ = as_user(c, "alice")
    c.post("/api/rooms/join", json={"room_code": code}, headers=h2)
    assert c.patch(f"/api/rooms/{rid}", json={"locked": True}, headers=h2).status_code == 403
    assert c.post(f"/api/rooms/{rid}/kick", json={"user_id": "adm"}, headers=h2).status_code == 403
    assert c.post(f"/api/rooms/{rid}/end", headers=h2).status_code == 403
    register(c, "bob"); as_user(c, "bob")
    assert c.get(f"/api/rooms/{rid}").status_code == 404                                        # non-member


def test_join_rate_limit(env):
    c, *_ = env
    register(c, "alice"); h, _ = as_user(c, "alice")
    codes = [c.post("/api/rooms/join", json={"room_code": "AAAAAAAA"}, headers=h).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]


def test_rooms_restored_after_restart(env):
    c, app, db, tmp = env
    _, mid, r = make(c)
    rid, code = r.json()["id"], r.json()["room_code"]
    register(c, "alice"); h2, _ = as_user(c, "alice")
    c.post("/api/rooms/join", json={"room_code": code}, headers=h2)
    uid = c.get("/api/auth/me").json()["user"]["id"]
    app2 = mk(tmp, db)
    with TestClient(app2):
        assert uid in app2.state.rooms[rid].allowed and app2.state.rooms[rid].room_code == code
