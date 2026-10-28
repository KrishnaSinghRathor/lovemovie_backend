import pytest
from test_sync import env, conn, until          # WebSocket fixtures: fake clock, admin / u1 / u2
from test_rooms import env as renv, make, as_user   # REST fixtures
from test_auth_movies import register
from app.api.subtitles import to_vtt

SRT = "1\n00:00:01,000 --> 00:00:02,500\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n"


def test_chat(env):
    c, clock, _ = env
    with conn(c, "sa") as a, conn(c, "s1") as p:
        until(a, "ROOM_STATE"); until(p, "ROOM_STATE")
        p.send_json({"type": "CHAT", "text": "  hello  "})
        for w in (a, p):
            m = until(w, "CHAT_MESSAGE")
            assert m["text"] == "hello" and m["username"] == "u1"
        for bad in ("", "   ", "x" * 501, 5, None):
            p.send_json({"type": "CHAT", "text": bad})
            assert until(p, "ERROR")["code"] == "BAD_MESSAGE"
        for _ in range(4):
            p.send_json({"type": "CHAT", "text": "m"}); until(p, "CHAT_MESSAGE")
        p.send_json({"type": "CHAT", "text": "too many"})
        assert until(p, "ERROR")["code"] == "RATE_LIMITED"
        clock.t += 11
        p.send_json({"type": "CHAT", "text": "again"}); until(p, "CHAT_MESSAGE")
        with conn(c, "s2") as q:
            hist = until(q, "ROOM_STATE")["chat"]
        assert hist[0]["text"] == "hello" and len(hist) == 6          # late joiner gets history
        p.send_json({"type": "CHAT_DELETE", "id": hist[0]["id"]})
        assert until(p, "ERROR")["code"] == "FORBIDDEN"
        a.send_json({"type": "CHAT_DELETE", "id": hist[0]["id"]})
        assert until(p, "CHAT_DELETED")["id"] == hist[0]["id"]


def test_voice_signaling_and_mute(env):
    c, _, _ = env
    with conn(c, "sa") as a, conn(c, "s1") as p, conn(c, "s2") as q:
        for w in (a, p, q): until(w, "ROOM_STATE")
        a.send_json({"type": "VOICE_JOIN"}); assert until(a, "VOICE_PEERS")["peers"] == []
        p.send_json({"type": "VOICE_JOIN"}); assert until(p, "VOICE_PEERS")["peers"] == ["admin"]
        assert until(a, "VOICE_JOINED")["user_id"] == "u1"
        p.send_json({"type": "VOICE_SIGNAL", "to": "admin", "data": {"sdp": {"type": "offer", "sdp": "x"}}})
        m = until(a, "VOICE_SIGNAL")
        assert m["from"] == "u1" and m["data"]["sdp"]["type"] == "offer"
        for w, to, data in ((q, "admin", {}), (p, "u2", {}), (p, "admin", {"x": "y" * 20000}), (p, ["admin"], {})):
            w.send_json({"type": "VOICE_SIGNAL", "to": to, "data": data})
            assert until(w, "ERROR")["code"] == "BAD_SIGNAL"          # not in voice / too big / malformed
        p.send_json({"type": "VOICE_MUTE", "user_id": "admin"})
        assert until(p, "ERROR")["code"] == "FORBIDDEN"
        a.send_json({"type": "VOICE_MUTE", "user_id": "u1"}); until(p, "VOICE_FORCE_MUTE")


def test_voice_cleared_on_disconnect(env):
    c, _, room = env
    with conn(c, "sa") as a:
        until(a, "ROOM_STATE")
        with conn(c, "s1") as p:
            until(p, "ROOM_STATE"); p.send_json({"type": "VOICE_JOIN"}); until(p, "VOICE_PEERS"); until(a, "VOICE_JOINED")
        assert until(a, "VOICE_LEFT")["user_id"] == "u1" and "u1" not in room.voice


def test_srt_to_vtt():
    v = to_vtt("\ufeff" + SRT.replace("\n", "\r\n"))
    assert v.startswith("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\nHello")
    assert to_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi").count("WEBVTT") == 1


def test_subtitles_flow(renv):
    c, app, db, tmp = renv
    h, mid, r = make(c)
    rid, code = r.json()["id"], r.json()["room_code"]
    url = f"/api/movies/{mid}/subtitles"
    post = lambda hh, **kw: c.post(url, json={"label": "English", "lang": "en", "content": SRT} | kw, headers=hh)
    assert post(h).status_code == 201
    assert post(h, content="just some text").status_code == 422
    assert post(h, lang="en us!").status_code == 422
    sid = c.get(url).json()[0]["id"]
    register(c, "alice"); ha, _ = as_user(c, "alice")
    assert post(ha).status_code == 403
    assert c.get(url).status_code == 404 and c.get(f"/api/subtitles/{sid}").status_code == 404      # not a member
    assert c.post("/api/rooms/join", json={"room_code": code}, headers=ha).status_code == 200
    r = c.get(f"/api/subtitles/{sid}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt") and r.text.startswith("WEBVTT")
    assert c.delete(f"/api/subtitles/{sid}", headers=ha).status_code == 403
    h, _ = as_user(c, "boss")
    assert c.patch(f"/api/rooms/{rid}", json={"allow_downloads": False}, headers=h).status_code == 200
    as_user(c, "alice")
    assert c.get(url).status_code == 200                                    # subtitles stay visible without downloads
    h, _ = as_user(c, "boss")
    assert c.delete(f"/api/subtitles/{sid}", headers=h).status_code == 200 and c.get(url).json() == []
