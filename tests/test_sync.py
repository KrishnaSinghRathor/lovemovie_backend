import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.main import create_app
from app.core.config import Settings
from mongomock_motor import AsyncMongoMockClient
app = create_app(db=AsyncMongoMockClient()["t"], settings=Settings(storage_dir="/tmp/sc_old"))
from app.sync.engine import PlaybackState, SyncError
from app.websocket.hub import Room

CHK = "a" * 64


class Clock:
    t = 1000.0
    def __call__(self): return self.t


@pytest.fixture
def env():
    clock = Clock()
    st = PlaybackState("r1", "m1", duration=7200.0)
    room = Room(st, "admin", CHK, clock, grace=0.05)
    room.allowed |= {"u1", "u2"}
    app.state.rooms = {"r1": room}
    app.state.sessions = {s: {"user_id": u, "username": u} for s, u in
                          [("sa", "admin"), ("s1", "u1"), ("s2", "u2"), ("sx", "outsider")]}
    with TestClient(app) as c:
        yield c, clock, room


def conn(c, sid):
    return c.websocket_connect("/ws/rooms/r1", headers={"cookie": f"sid={sid}"})


def until(ws, typ, n=10):
    for _ in range(n):
        m = ws.receive_json()
        if m["type"] == typ:
            return m
    raise AssertionError(f"no {typ}")


def test_admin_pause_resume(env):
    c, clock, _ = env
    with conn(c, "sa") as a, conn(c, "s1") as p:
        until(a, "ROOM_STATE"); until(p, "ROOM_STATE")
        a.send_json({"type": "PLAY", "seq": 1})
        assert until(p, "PLAYBACK_UPDATE")["state"]["state"] == "PLAYING"
        clock.t += 30
        a.send_json({"type": "PAUSE", "seq": 2})
        s = until(p, "PLAYBACK_UPDATE")["state"]
        assert s["state"] == "PAUSED" and s["position"] == 30.0
        a.send_json({"type": "PLAY", "seq": 3})
        s = until(p, "PLAYBACK_UPDATE")["state"]
        assert s["state"] == "PLAYING" and s["position"] == 30.0


def test_participant_cannot_control(env):
    c, _, room = env
    with conn(c, "s1") as p:
        until(p, "ROOM_STATE")
        for cmd in ("PLAY", "PAUSE", "SEEK", "RESTART", "RATE", "END"):
            p.send_json({"type": cmd, "seq": 99, "value": 10})
            assert until(p, "ERROR")["code"] == "FORBIDDEN"
        assert room.state.version == 0 and room.state.state == "PAUSED"


def test_seek_syncs_and_validates(env):
    c, _, room = env
    with conn(c, "sa") as a, conn(c, "s1") as p1, conn(c, "s2") as p2:
        a.send_json({"type": "SEEK", "seq": 1, "value": 1935.5})
        for p in (p1, p2):
            assert until(p, "PLAYBACK_UPDATE")["state"]["position"] == 1935.5
        for bad in (-1, 7200.1, "x", None, float("inf")):
            a.send_json({"type": "SEEK", "seq": room.admin_seq + 1, "value": bad})
            assert until(a, "ERROR")["code"] == "INVALID_POSITION"
        assert room.state.version == 1


def test_late_join_extrapolates(env):
    c, clock, _ = env
    with conn(c, "sa") as a:
        a.send_json({"type": "SEEK", "seq": 1, "value": 100}); until(a, "PLAYBACK_UPDATE")
        a.send_json({"type": "PLAY", "seq": 2}); until(a, "PLAYBACK_UPDATE")
        clock.t += 42
        with conn(c, "s1") as p:
            s = until(p, "ROOM_STATE")["state"]
            assert s["state"] == "PLAYING" and s["current_position"] == pytest.approx(142.0)


def test_reconnect_resyncs_and_stale_rejected(env):
    c, clock, room = env
    with conn(c, "sa") as a:
        a.send_json({"type": "PLAY", "seq": 5}); until(a, "PLAYBACK_UPDATE")
        a.send_json({"type": "PAUSE", "seq": 5})            # replayed/stale
        assert until(a, "ERROR")["code"] == "STALE"
        a.send_json({"type": "PAUSE", "seq": 4})
        assert until(a, "ERROR")["code"] == "STALE"
    with conn(c, "s1") as p:
        clock.t += 10
        with conn(c, "s1") as p_again:                      # reconnect replaces old socket
            m = until(p_again, "ROOM_STATE")
            assert m["state"]["current_position"] == pytest.approx(10.0)
            assert m["admin_seq"] == 5


def test_rapid_commands_consistent(env):
    c, _, room = env
    with conn(c, "sa") as a, conn(c, "s1") as p:
        for i in range(1, 21):
            a.send_json({"type": "SEEK" if i % 2 else "PAUSE", "seq": i, "value": i})
        vs = [until(p, "PLAYBACK_UPDATE")["state"]["version"] for _ in range(20)]
        assert vs == list(range(1, 21)) and room.state.version == 20


def test_corrupt_file_not_verified(env):
    c, _, _ = env
    with conn(c, "s1") as p:
        until(p, "ROOM_STATE")
        p.send_json({"type": "READY", "checksum": "b" * 64})
        assert until(p, "ROOM_STATE")["verified"] is False
        p.send_json({"type": "READY", "checksum": CHK})
        assert until(p, "ROOM_STATE")["verified"] is True


def test_unauthorized_rejected(env):
    c, _, _ = env
    for sid in ("sx", "nope"):
        with pytest.raises(WebSocketDisconnect):
            with conn(c, sid): pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/rooms/r1", headers={"cookie": "sid=s1", "origin": "http://evil.example"}): pass


def test_admin_disconnect_grace_pauses(env):
    import time
    c, _, room = env
    with conn(c, "s1") as p:
        until(p, "ROOM_STATE")
        with conn(c, "sa") as a:
            a.send_json({"type": "PLAY", "seq": 1}); until(p, "PLAYBACK_UPDATE")
        assert until(p, "PLAYBACK_UPDATE")["cause"] == "ADMIN_DISCONNECTED"
        assert room.state.state == "PAUSED"


def test_engine_rate_and_end():
    s = PlaybackState("r", "m", 100.0)
    s.apply("PLAY", 0); s.apply("RATE", 10, 2.0)
    assert s.position == 10 and s.position_at(20) == 30
    with pytest.raises(SyncError): s.apply("RATE", 11, 5)
    s.apply("END", 20)
    with pytest.raises(SyncError): s.apply("PLAY", 21)
