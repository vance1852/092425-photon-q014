from __future__ import annotations

import base64
import json
import unittest

from photon_fab.service import PhotonService, _encode_audit_cursor


class AuditExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")

    def make_lot(self, lot_id: str, events: int) -> None:
        self.service.create_lot(self.token, lot_id, "CMOS image sensor", "P3.2", 10)
        for index in range(events - 1):
            self.service.add_measurement(self.token, lot_id, 450.0 + index, 0.9, 0.01, "spectrometer-1")

    def export_all(self, lot_id: str, limit: int) -> list[dict]:
        cursor = None
        events: list[dict] = []
        while True:
            page = self.service.export_audit(self.token, lot_id, cursor, limit)
            events.extend(page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                return events

    def test_pages_reassemble_full_sequence_in_order(self) -> None:
        self.make_lot("LOT-A", 25)
        pages = []
        cursor = None
        while True:
            page = self.service.export_audit(self.token, "LOT-A", cursor, 10)
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual([len(page["events"]) for page in pages], [10, 10, 5])
        self.assertTrue(all(page["has_more"] for page in pages[:-1]))
        self.assertFalse(pages[-1]["has_more"])
        self.assertIsNone(pages[-1]["next_cursor"])
        flattened = [event for page in pages for event in page["events"]]
        self.assertEqual(flattened, self.service.audit(self.token, "LOT-A"))
        ids = [event["event_id"] for event in flattened]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))

    def test_same_cursor_replay_returns_identical_page(self) -> None:
        self.make_lot("LOT-B", 15)
        first = self.service.export_audit(self.token, "LOT-B", None, 10)
        replay_a = self.service.export_audit(self.token, "LOT-B", first["next_cursor"], 10)
        self.make_lot("LOT-B2", 3)  # 无关批次写入
        self.service.add_measurement(self.token, "LOT-B", 700.0, 0.5, 0.01, "spectrometer-1")
        replay_b = self.service.export_audit(self.token, "LOT-B", first["next_cursor"], 10)
        self.assertEqual(replay_a, replay_b)
        self.assertEqual([e["event_id"] for e in replay_a["events"]], list(range(11, 16)))

    def test_appends_during_export_neither_duplicate_nor_omit(self) -> None:
        self.make_lot("LOT-C", 20)
        baseline_ids = [e["event_id"] for e in self.service.audit(self.token, "LOT-C")]
        first = self.service.export_audit(self.token, "LOT-C", None, 8)
        for index in range(5):
            self.service.add_measurement(self.token, "LOT-C", 800.0 + index, 0.5, 0.01, "spectrometer-2")
        cursor = first["next_cursor"]
        collected = list(first["events"])
        while cursor is not None:
            page = self.service.export_audit(self.token, "LOT-C", cursor, 8)
            collected.extend(page["events"])
            cursor = page["next_cursor"]
        collected_ids = [event["event_id"] for event in collected]
        self.assertEqual(collected_ids, baseline_ids)
        self.assertEqual(len(set(collected_ids)), len(collected_ids))
        fresh_ids = [e["event_id"] for e in self.export_all("LOT-C", 8)]
        self.assertEqual(len(fresh_ids), 25)
        self.assertEqual(fresh_ids[:20], baseline_ids)

    def test_cursor_bound_to_originating_lot(self) -> None:
        self.make_lot("LOT-D", 12)
        self.make_lot("LOT-E", 12)
        cursor = self.service.export_audit(self.token, "LOT-D", None, 5)["next_cursor"]
        with self.assertRaises(ValueError):
            self.service.export_audit(self.token, "LOT-E", cursor, 5)
        for event in self.export_all("LOT-E", 5):
            self.assertEqual(event["lot_id"], "LOT-E")

    def test_invalid_cursor_rejected(self) -> None:
        self.make_lot("LOT-F", 3)
        for bad in ("not-a-cursor", "", "!!!"):
            with self.assertRaises(ValueError, msg=bad):
                self.service.export_audit(self.token, "LOT-F", bad, 10)
        wrong_version = base64.urlsafe_b64encode(b'{"v":2,"lot":"LOT-F","after":0,"max":1}').decode()
        with self.assertRaises(ValueError):
            self.service.export_audit(self.token, "LOT-F", wrong_version, 10)
        forged = _encode_audit_cursor("LOT-OTHER", 0, 10)
        with self.assertRaises(ValueError):
            self.service.export_audit(self.token, "LOT-F", forged, 10)

    def test_limit_validation(self) -> None:
        self.make_lot("LOT-G", 3)
        for bad in (0, -1, 501, True, "10", 1.5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.service.export_audit(self.token, "LOT-G", None, bad)

    def test_unknown_lot_and_bad_token(self) -> None:
        with self.assertRaises(KeyError):
            self.service.export_audit(self.token, "LOT-MISSING")
        self.make_lot("LOT-H", 2)
        with self.assertRaises(PermissionError):
            self.service.export_audit("bogus-token", "LOT-H")

    def test_audit_matches_paged_export(self) -> None:
        self.make_lot("LOT-I", 7)
        self.assertEqual(self.service.audit(self.token, "LOT-I"), self.export_all("LOT-I", 3))


if __name__ == "__main__":
    unittest.main()
