from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from app.config import IDLE_TIMEOUT_SEC


@dataclass
class Session:
    id: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    station_id: str | None = None

    def touch(self) -> None:
        self.last_seen = time.time()


class SessionStore:
    def __init__(self, idle_timeout: int = IDLE_TIMEOUT_SEC) -> None:
        self.idle_timeout = idle_timeout
        self.sessions: dict[str, Session] = {}
        self.claims: dict[str, str] = {}  # station_id -> session_id

    def get_or_create(self, session_id: str | None) -> tuple[Session, bool]:
        if session_id and session_id in self.sessions:
            sess = self.sessions[session_id]
            sess.touch()
            return sess, False
        sess = Session(id=uuid.uuid4().hex)
        self.sessions[sess.id] = sess
        return sess, True

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def claimant(self, station_id: str) -> str | None:
        return self.claims.get(station_id)

    def is_owner(self, station_id: str, session_id: str) -> bool:
        return self.claims.get(station_id) == session_id

    def claim(self, station_id: str, session: Session) -> Session:
        owner = self.claims.get(station_id)
        if owner and owner != session.id:
            other = self.sessions.get(owner)
            if other and (time.time() - other.last_seen) < self.idle_timeout:
                raise PermissionError("Station is in use")
            # stale claim
            if other:
                other.station_id = None
        if session.station_id and session.station_id != station_id:
            self.release(session.station_id, session)
        self.claims[station_id] = session.id
        session.station_id = station_id
        session.touch()
        return session

    def release(self, station_id: str, session: Session | None = None) -> None:
        owner = self.claims.get(station_id)
        if session and owner and owner != session.id:
            raise PermissionError("Not the claimant")
        self.claims.pop(station_id, None)
        if owner and owner in self.sessions:
            self.sessions[owner].station_id = None
        if session:
            session.station_id = None

    def expire_idle(self) -> list[str]:
        now = time.time()
        released = []
        for station_id, sid in list(self.claims.items()):
            sess = self.sessions.get(sid)
            if not sess or (now - sess.last_seen) > self.idle_timeout:
                self.claims.pop(station_id, None)
                if sess:
                    sess.station_id = None
                released.append(station_id)
        return released

    def public_claim(self, station_id: str, viewer: Session | None) -> dict:
        owner = self.claims.get(station_id)
        if not owner:
            return {"claimed": False, "mine": False}
        sess = self.sessions.get(owner)
        idle = (time.time() - sess.last_seen) if sess else 0
        return {
            "claimed": True,
            "mine": bool(viewer and viewer.id == owner),
            "idle_sec": int(idle),
        }
