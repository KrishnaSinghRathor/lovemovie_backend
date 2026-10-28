"""Authoritative playback state machine (pure logic, no I/O)."""
import math
from dataclasses import dataclass

COMMANDS = {"PLAY", "PAUSE", "SEEK", "RESTART", "RATE", "END"}
MIN_RATE, MAX_RATE = 0.25, 2.0


class SyncError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass
class PlaybackState:
    room_id: str
    movie_id: str
    duration: float
    state: str = "PAUSED"  # PLAYING | PAUSED | ENDED
    position: float = 0.0  # anchor position at server_ts
    rate: float = 1.0
    version: int = 0
    server_ts: float = 0.0

    def position_at(self, now: float) -> float:
        if self.state != "PLAYING":
            return self.position
        return min(self.duration, self.position + max(0.0, now - self.server_ts) * self.rate)

    def snapshot(self, now: float) -> dict:
        return {
            "room_id": self.room_id, "movie_id": self.movie_id, "state": self.state,
            "position": self.position, "playback_rate": self.rate, "version": self.version,
            "server_timestamp": self.server_ts, "server_now": now,
            "current_position": self.position_at(now), "duration": self.duration,
        }

    def apply(self, cmd: str, now: float, value=None) -> None:
        pos = self.position_at(now)
        if cmd == "PLAY":
            if self.state == "ENDED":
                raise SyncError("INVALID_STATE", "Playback has ended")
            self.position = 0.0 if pos >= self.duration else pos
            self.state = "PLAYING"
        elif cmd == "PAUSE":
            self.position, self.state = pos, "PAUSED"
        elif cmd == "SEEK":
            if not _num(value) or not 0 <= value <= self.duration:
                raise SyncError("INVALID_POSITION", f"Position must be within 0..{self.duration}")
            self.position = float(value)
            if self.state == "ENDED":
                self.state = "PAUSED"
        elif cmd == "RESTART":
            self.position, self.state = 0.0, "PLAYING"
        elif cmd == "RATE":
            if not _num(value) or not MIN_RATE <= value <= MAX_RATE:
                raise SyncError("INVALID_RATE", f"Rate must be {MIN_RATE}..{MAX_RATE}")
            self.position, self.rate = pos, float(value)
        elif cmd == "END":
            self.position, self.state = pos, "ENDED"
        else:
            raise SyncError("UNKNOWN_COMMAND", cmd)
        self.version += 1
        self.server_ts = now
