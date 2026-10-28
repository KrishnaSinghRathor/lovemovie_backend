"""Room runtime + WebSocket protocol. Roles come from the server session, never the client."""
import asyncio, json
from dataclasses import dataclass
from fastapi import WebSocket, WebSocketDisconnect
from ..api.deps import resolve_session
from ..sync.engine import COMMANDS, PlaybackState, SyncError


@dataclass
class Conn:
    ws: WebSocket
    user_id: str
    username: str
    is_admin: bool
    verified: bool = False


class Room:
    def __init__(self, state: PlaybackState, admin_id: str, checksum: str, clock, grace: float = 10.0):
        self.state, self.admin_id, self.checksum = state, admin_id, checksum
        self.clock, self.grace = clock, grace
        self.allowed = {admin_id}   # filled by the REST join endpoint (password/lock checks)
        self.conns: dict[str, Conn] = {}
        self.status, self.admin_seq, self._grace_task = "open", 0, None
        self.locked, self.allow_downloads, self.banned = False, True, set()
        self.room_code, self.password_hash, self.admin_name = "", None, ""
        self.chat, self.voice, self.chat_hits, self._chat_seq = [], set(), {}, 0

    def _snap(self):
        return self.state.snapshot(self.clock())

    def _participants(self):
        return [{"user_id": c.user_id, "username": c.username, "is_admin": c.is_admin, "verified": c.verified}
                for c in self.conns.values()]

    async def _send(self, conn: Conn, payload: dict):
        try:
            await conn.ws.send_json(payload)
        except Exception:
            pass

    async def broadcast(self, payload: dict, exclude: str | None = None):
        for uid, c in list(self.conns.items()):
            if uid != exclude:
                await self._send(c, payload)

    async def _room_state(self, conn: Conn):
        await self._send(conn, {"type": "ROOM_STATE", "state": self._snap(), "role": "admin" if conn.is_admin else "participant",
                                "verified": conn.verified, "participants": self._participants(), "admin_seq": self.admin_seq,
                                "chat": self.chat, "voice": list(self.voice), "you": conn.user_id})

    async def connect(self, ws: WebSocket, ident: dict):
        uid = ident["user_id"]
        old = self.conns.get(uid)
        if old:
            try: await old.ws.close(code=4000)
            except Exception: pass
        conn = Conn(ws, uid, ident["username"], uid == self.admin_id)
        self.conns[uid] = conn
        if uid in self.voice:   # reconnect: the old socket's voice session is gone
            self.voice.discard(uid)
            await self.broadcast({"type": "VOICE_LEFT", "user_id": uid}, exclude=uid)
        if conn.is_admin and self._grace_task:
            self._grace_task.cancel(); self._grace_task = None
        await self._room_state(conn)
        await self.broadcast({"type": "PARTICIPANT_JOINED", "user_id": uid, "username": conn.username}, exclude=uid)
        return conn

    async def leave(self, conn: Conn):
        if self.conns.get(conn.user_id) is not conn:
            return  # superseded by a reconnect
        del self.conns[conn.user_id]
        await self.broadcast({"type": "PARTICIPANT_LEFT", "user_id": conn.user_id})
        await self._voice_leave(conn.user_id)
        if conn.is_admin and self.state.state == "PLAYING":
            self._grace_task = asyncio.create_task(self._grace_pause())

    async def _grace_pause(self):
        await asyncio.sleep(self.grace)
        if self.admin_id not in self.conns and self.state.state == "PLAYING":
            self.state.apply("PAUSE", self.clock())
            await self.broadcast({"type": "PLAYBACK_UPDATE", "cause": "ADMIN_DISCONNECTED", "state": self._snap()})

    async def _voice_leave(self, uid: str):
        if uid in self.voice:
            self.voice.discard(uid)
            await self.broadcast({"type": "VOICE_LEFT", "user_id": uid})

    async def kick(self, uid: str):
        self.allowed.discard(uid)
        c = self.conns.pop(uid, None)
        if c:
            await self._send(c, {"type": "ERROR", "code": "REMOVED", "message": "You were removed from the room"})
            try: await c.ws.close(code=4403)
            except Exception: pass
            await self.broadcast({"type": "PARTICIPANT_LEFT", "user_id": uid})
            await self._voice_leave(uid)

    async def end(self):
        self.status = "ended"
        await self.broadcast({"type": "ROOM_ENDED"})
        for c in list(self.conns.values()):
            try: await c.ws.close(code=4000)
            except Exception: pass
        self.conns.clear()
        self.voice.clear()

    async def dispatch(self, conn: Conn, msg: dict):
        t = msg.get("type")
        if t == "PING":
            return await self._send(conn, {"type": "PONG", "server_now": self.clock()})
        if t == "SYNC_REQUEST":
            return await self._send(conn, {"type": "SYNC_RESPONSE", "state": self._snap()})
        if t == "READY":  # participant reports SHA-256 of its local copy
            conn.verified = msg.get("checksum") == self.checksum
            await self._room_state(conn)
            return await self.broadcast({"type": "PARTICIPANT_UPDATED", "user_id": conn.user_id, "verified": conn.verified})
        uid = conn.user_id

        async def err(code, text):
            await self._send(conn, {"type": "ERROR", "code": code, "message": text})

        if t == "CHAT":
            text, now = msg.get("text"), self.clock()
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 500:
                return await err("BAD_MESSAGE", "Message must be 1-500 characters")
            hits = [h for h in self.chat_hits.get(uid, []) if now - h < 10]
            if len(hits) >= 5:
                return await err("RATE_LIMITED", "You are sending messages too fast")
            self.chat_hits[uid] = hits + [now]
            self._chat_seq += 1
            m = {"id": str(self._chat_seq), "user_id": uid, "username": conn.username, "text": text.strip(), "ts": now}
            self.chat = (self.chat + [m])[-100:]
            return await self.broadcast({"type": "CHAT_MESSAGE", **m})
        if t == "CHAT_DELETE":
            if not conn.is_admin:
                return await err("FORBIDDEN", "Only the admin can delete messages")
            self.chat = [m for m in self.chat if m["id"] != msg.get("id")]
            return await self.broadcast({"type": "CHAT_DELETED", "id": msg.get("id")})
        if t == "VOICE_JOIN":
            peers = [u for u in self.voice if u != uid and u in self.conns]
            self.voice.add(uid)
            await self._send(conn, {"type": "VOICE_PEERS", "peers": peers})
            return await self.broadcast({"type": "VOICE_JOINED", "user_id": uid}, exclude=uid)
        if t == "VOICE_LEAVE":
            return await self._voice_leave(uid)
        if t == "VOICE_SIGNAL":     # WebRTC setup data relayed between two voice participants only
            to, data = msg.get("to"), msg.get("data")
            if not isinstance(to, str) or not isinstance(data, dict) or len(json.dumps(data)) > 16384 \
                    or uid not in self.voice or to not in self.voice or to not in self.conns:
                return await err("BAD_SIGNAL", "Cannot deliver signal")
            return await self._send(self.conns[to], {"type": "VOICE_SIGNAL", "from": uid, "data": data})
        if t == "VOICE_MUTE":
            if not conn.is_admin:
                return await err("FORBIDDEN", "Only the admin can mute others")
            tgt = self.conns.get(msg.get("user_id"))
            if tgt:
                await self._send(tgt, {"type": "VOICE_FORCE_MUTE"})
            return
        if t in COMMANDS:
            if not conn.is_admin:
                return await self._send(conn, {"type": "ERROR", "code": "FORBIDDEN", "message": "Only the admin controls playback"})
            seq = msg.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= self.admin_seq:
                return await self._send(conn, {"type": "ERROR", "code": "STALE", "message": "Stale command", "state": self._snap()})
            try:
                self.state.apply(t, self.clock(), msg.get("value"))
            except SyncError as e:
                return await self._send(conn, {"type": "ERROR", "code": e.code, "message": e.message, "state": self._snap()})
            self.admin_seq = seq
            return await self.broadcast({"type": "PLAYBACK_UPDATE", "cause": t, "state": self._snap()})
        await self._send(conn, {"type": "ERROR", "code": "UNKNOWN_TYPE", "message": "Unknown message type"})


async def handle_ws(ws: WebSocket, room_id: str):
    app = ws.app
    origin = ws.headers.get("origin")
    if origin and origin not in app.state.allowed_origins:
        return await ws.close(code=4403)
    ident = await resolve_session(app, ws.cookies.get("sid", ""))
    room: Room | None = app.state.rooms.get(room_id)
    if not ident or not room or room.status != "open" or ident["user_id"] not in room.allowed:
        return await ws.close(code=4401)
    await ws.accept()
    conn = await room.connect(ws, ident)
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
                if not isinstance(msg, dict): raise ValueError
            except ValueError:
                await room._send(conn, {"type": "ERROR", "code": "BAD_MESSAGE", "message": "Invalid JSON"})
                continue
            await room.dispatch(conn, msg)
    except WebSocketDisconnect:
        pass
    finally:
        await asyncio.shield(room.leave(conn))   # cleanup must finish even if this handler is cancelled
