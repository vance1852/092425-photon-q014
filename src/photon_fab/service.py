"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import connect, event, transaction, utcnow

AUDIT_PAGE_DEFAULT = 100
AUDIT_PAGE_MAX = 500


def _encode_audit_cursor(lot_id: str, after_id: int, snapshot_max: int) -> str:
    raw = json.dumps(
        {"v": 1, "lot": lot_id, "after": after_id, "max": snapshot_max},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_audit_cursor(cursor: str) -> tuple[str, int, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError
        lot_id = data["lot"]
        after_id = data["after"]
        snapshot_max = data["max"]
        if (
            not isinstance(lot_id, str)
            or not isinstance(after_id, int)
            or not isinstance(snapshot_max, int)
            or isinstance(after_id, bool)
            or isinstance(snapshot_max, bool)
            or after_id < 0
            or snapshot_max < 0
            or after_id > snapshot_max
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("audit cursor is invalid") from exc
    return lot_id, after_id, snapshot_max


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def export_audit(self, token: str, lot_id: str, cursor: str | None = None, limit: int = AUDIT_PAGE_DEFAULT) -> dict:
        """按稳定游标分页导出批次审计事件。

        事件按 event_id 升序排列；游标记录上一页末尾的 event_id 和本次导出
        的快照上界（首页请求时的最大 event_id），因此相同游标重复请求返回同
        一页，导出期间新增的事件不会串入本次导出，跨页不重复也不遗漏。
        """
        self.auth.require(token, "read")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= AUDIT_PAGE_MAX:
            raise ValueError(f"limit must be an integer between 1 and {AUDIT_PAGE_MAX}")
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)
        if cursor is None:
            after_id = 0
            snapshot_max = self.db.execute(
                "SELECT COALESCE(MAX(event_id),0) FROM lot_events WHERE lot_id=?", (lot_id,)
            ).fetchone()[0]
        else:
            cursor_lot, after_id, snapshot_max = _decode_audit_cursor(cursor)
            if cursor_lot != lot_id:
                raise ValueError("audit cursor belongs to a different lot")
        rows = self.db.execute(
            "SELECT * FROM lot_events WHERE lot_id=? AND event_id>? AND event_id<=? "
            "ORDER BY event_id LIMIT ?",
            (lot_id, after_id, snapshot_max, limit + 1),
        ).fetchall()
        page = [dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = _encode_audit_cursor(lot_id, page[-1]["event_id"], snapshot_max)
        return {
            "lot_id": lot_id,
            "events": page,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }

    def audit(self, token: str, lot_id: str) -> list[dict]:
        cursor = None
        events: list[dict] = []
        while True:
            page = self.export_audit(token, lot_id, cursor, AUDIT_PAGE_MAX)
            events.extend(page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                return events
