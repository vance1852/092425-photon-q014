"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import connect, event, transaction, utcnow


DEFAULT_AUDIT_PAGE_SIZE = 50
MAX_AUDIT_PAGE_SIZE = 200


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

    def audit(self, token: str, lot_id: str) -> list[dict]:
        """读取批次的完整审计事件流（向后兼容的便捷封装）。"""

        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)
        ).fetchall()]

    def audit_page(
        self,
        token: str,
        lot_id: str,
        limit: int = DEFAULT_AUDIT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict:
        """按键集游标稳定读取一页审计事件。

        游标对 (lot_id, 上一页末事件 event_id, 首次导出时的批次快照高水位)
        做防篡改编码：同一游标重复请求永远返回同一页；新写入的事件只会追加在
        快照之后，既不会让后续页整体后移（偏移分页会因此重复或漏项），也不会跨
        进更早的页。批次隔离由游标中绑定的 lot_id 与查询条件双重保证，其他批次
        的事件不可能串入结果。
        """

        self.auth.require(token, "read")
        page_size = self._page_size(limit)

        if cursor is None:
            snapshot = self._snapshot_high_watermark(lot_id)
            after_id = 0
        else:
            cursor_lot_id, after_id, snapshot = self._decode_cursor(cursor)
            if cursor_lot_id != lot_id:
                raise ValueError("cursor does not belong to this lot")

        rows = self.db.execute(
            "SELECT * FROM lot_events WHERE lot_id=? AND event_id>? AND event_id<=? "
            "ORDER BY event_id ASC LIMIT ?",
            (lot_id, after_id, snapshot, page_size + 1),
        ).fetchall()
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        events = [dict(r) for r in page_rows]
        last_id = int(page_rows[-1]["event_id"]) if page_rows else after_id
        next_cursor = (
            self._encode_cursor(lot_id, last_id, snapshot) if has_more else None
        )
        return {
            "lot_id": lot_id,
            "events": events,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    @staticmethod
    def _page_size(limit: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be a positive integer") from exc
        if value <= 0:
            raise ValueError("limit must be a positive integer")
        return min(value, MAX_AUDIT_PAGE_SIZE)

    def _snapshot_high_watermark(self, lot_id: str) -> int:
        """导出起始时刻该批次可见的最大 event_id；批次不存在同样报错。"""

        row = self.db.execute(
            "SELECT COALESCE(MAX(event_id), 0) AS high_watermark FROM lot_events WHERE lot_id=?",
            (lot_id,),
        ).fetchone()
        # 同时校验批次存在，避免对不存在的批次静默导出空页。
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)
        return int(row["high_watermark"])

    @staticmethod
    def _encode_cursor(lot_id: str, after_id: int, snapshot: int) -> str:
        raw = json.dumps(
            {"lot_id": lot_id, "after_id": int(after_id), "snapshot": int(snapshot)},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, int, int]:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
            lot_id = str(payload["lot_id"])
            after_id = int(payload["after_id"])
            snapshot = int(payload["snapshot"])
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("invalid cursor") from exc
        if after_id < 0 or snapshot < after_id:
            raise ValueError("invalid cursor")
        return lot_id, after_id, snapshot
