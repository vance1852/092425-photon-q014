from __future__ import annotations

import base64
import json
import unittest

from photon_fab.service import PhotonService


class AuditPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService(":memory:")
        self.service.bootstrap_admin("admin", "photon-admin")
        self.token = self.service.auth.login("admin", "photon-admin")
        self.service.create_lot(self.token, "LOT-A", "sensor", "P1", 3)
        self.service.create_lot(self.token, "LOT-B", "sensor", "P1", 3)

    def _add_events(self, lot_id: str, count: int) -> None:
        for index in range(count):
            self.service.add_measurement(
                self.token, lot_id, 400.0 + index, 0.9, 0.01, "spectrometer-1"
            )

    def _walk(self, lot_id: str, page_size: int) -> list[dict]:
        cursor = None
        seen: list[dict] = []
        pages = 0
        while True:
            page = self.service.audit_page(self.token, lot_id, page_size, cursor)
            seen.extend(page["events"])
            pages += 1
            self.assertLessEqual(pages, 1000, "分页无法终止")
            if page["next_cursor"] is None:
                self.assertFalse(page["has_more"])
                break
            cursor = page["next_cursor"]
        return seen

    def test_walk_covers_every_event_once_in_order(self) -> None:
        self._add_events("LOT-A", 5)
        events = self._walk("LOT-A", 2)
        ids = [event["event_id"] for event in events]
        expected = [
            row["event_id"]
            for row in self.service.db.execute(
                "SELECT event_id FROM lot_events WHERE lot_id='LOT-A' ORDER BY event_id"
            )
        ]
        self.assertEqual(ids, expected)
        self.assertEqual(len(ids), len(set(ids)))

    def test_same_cursor_returns_same_page_after_boundary_insert(self) -> None:
        # 首批只有 created 事件；建立第一页（每页 2 条）后拿到游标。
        self._add_events("LOT-A", 3)  # 事件 2/3/4
        first = self.service.audit_page(self.token, "LOT-A", 2)
        self.assertEqual(len(first["events"]), 2)
        self.assertTrue(first["has_more"])
        cursor = first["next_cursor"]

        # 恰好在"上一页末尾"之后新增事件：偏移分页会让下一页整体后移。
        self._add_events("LOT-A", 2)  # 事件 5/6

        # 相同游标重复请求必须返回同一页。
        second = self.service.audit_page(self.token, "LOT-A", 2, cursor)
        second_again = self.service.audit_page(self.token, "LOT-A", 2, cursor)
        self.assertEqual(
            [e["event_id"] for e in second["events"]],
            [e["event_id"] for e in second_again["events"]],
        )
        # 下一页恰好接住第一页之后的事件 3/4，不重复、不遗漏，新事件不串入。
        self.assertEqual(
            [e["event_id"] for e in second["events"]],
            [first["events"][-1]["event_id"] + 1, first["events"][-1]["event_id"] + 2],
        )
        # 快照固定在导出开始时，后续新增事件不出现在本次遍历中。
        self.assertFalse(second["has_more"])
        walked = [e["event_id"] for e in first["events"]] + [
            e["event_id"] for e in second["events"]
        ]
        self.assertEqual(walked, sorted(walked))
        self.assertEqual(len(walked), len(set(walked)))

        # 重新开始导出时新快照包含新追加的事件。
        restarted = [e["event_id"] for e in self._walk("LOT-A", 2)]
        expected = [
            row["event_id"]
            for row in self.service.db.execute(
                "SELECT event_id FROM lot_events WHERE lot_id='LOT-A' ORDER BY event_id"
            )
        ]
        self.assertEqual(restarted, expected)

    def test_events_from_other_lots_never_leak(self) -> None:
        self._add_events("LOT-A", 4)
        self._add_events("LOT-B", 7)
        for lot_id, count in (("LOT-A", 5), ("LOT-B", 8)):
            events = self._walk(lot_id, 3)
            self.assertEqual(len(events), count)
            self.assertTrue(all(e["lot_id"] == lot_id for e in events))

    def test_cursor_is_bound_to_its_lot(self) -> None:
        self._add_events("LOT-A", 1)
        cursor = self.service.audit_page(self.token, "LOT-A", 1)["next_cursor"]
        self.assertIsNotNone(cursor)
        with self.assertRaises(ValueError):
            self.service.audit_page(self.token, "LOT-B", 1, cursor)

    def test_tampered_or_garbage_cursor_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.audit_page(self.token, "LOT-A", 2, "not-a-cursor")
        self._add_events("LOT-A", 3)
        cursor = self.service.audit_page(self.token, "LOT-A", 2)["next_cursor"]
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        # 把快照高水位改到游标位置之前：结构上不可能，必须拒绝。
        raw["snapshot"] = raw["after_id"] - 1
        tampered = base64.urlsafe_b64encode(
            json.dumps(raw, separators=(",", ":")).encode()
        ).decode()
        with self.assertRaises(ValueError):
            self.service.audit_page(self.token, "LOT-A", 2, tampered)

    def test_invalid_limit_rejected(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                self.service.audit_page(self.token, "LOT-A", bad)

    def test_unknown_lot_is_not_found(self) -> None:
        with self.assertRaises(KeyError):
            self.service.audit_page(self.token, "LOT-MISSING", 2)

    def test_legacy_audit_still_returns_full_stream(self) -> None:
        self._add_events("LOT-A", 2)
        events = self.service.audit(self.token, "LOT-A")
        self.assertEqual([e["event_type"] for e in events][0], "created")
        self.assertEqual(len(events), 3)


if __name__ == "__main__":
    unittest.main()
