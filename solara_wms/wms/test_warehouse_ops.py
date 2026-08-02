from datetime import datetime, timedelta
from unittest import TestCase

from solara_wms.wms.warehouse_ops import build_metrics


class TestWarehouseOpsMetrics(TestCase):
    def test_lines_show_fair_rates_presence_and_completed_multibox_orders(self):
        now = datetime(2026, 8, 2, 18, 0)
        pack = [
            {"station": "Line 1", "delivery_note": "DN-1", "awb": "A1",
             "box_count": 1, "pieces_expected": 1, "mismatch": 0,
             "duration_sec": 30, "verified_at": now - timedelta(minutes=10)},
            {"station": "Line 2", "delivery_note": "DN-2", "awb": "B1",
             "box_count": 2, "pieces_expected": 4, "mismatch": 0,
             "duration_sec": 90, "verified_at": now - timedelta(minutes=20)},
            {"station": "Line 2", "delivery_note": "DN-2", "awb": "B2",
             "box_count": 2, "pieces_expected": 2, "mismatch": 0,
             "duration_sec": 60, "verified_at": now - timedelta(minutes=5)},
        ]

        out = build_metrics(pack, [], [], [], [], {"Line 1": now.isoformat()},
                            now, line_count=3)

        line1, line2, line3 = out["packing"]["lines"]
        self.assertEqual(line1["status"], "active")
        self.assertEqual(line1["parcels_per_hour"], 1)
        self.assertEqual(line2["orders"], 1)
        self.assertEqual(line2["parcels_per_hour"], 2)
        self.assertEqual(line2["pieces_per_hour"], 6)
        self.assertEqual(line2["avg_pieces"], 3)
        self.assertEqual(line3["status"], "offline")

    def test_quality_returns_dispatch_and_waiting_are_kept_separate(self):
        now = datetime(2026, 8, 2, 18, 0)
        pack = [
            {"station": "Line 1", "delivery_note": "DN-1", "awb": "A1",
             "box_count": 1, "pieces_expected": 3, "mismatch": 0,
             "verified_at": now - timedelta(minutes=30)},
            {"station": "Line 1", "delivery_note": "DN-2", "awb": "A2",
             "box_count": 1, "pieces_expected": 2, "mismatch": 1,
             "verified_at": now - timedelta(minutes=20)},
        ]
        returns = [{"name": "RET-1", "return_type": "RTO",
                    "received_at": now - timedelta(minutes=40),
                    "completed_at": now - timedelta(minutes=10)}]
        return_items = [{"parent": "RET-1", "condition": "Damaged"}]
        dispatch = [{"delivery_note": "DN-X", "awb": "X1", "courier": "Delhivery",
                     "scanned_at": now - timedelta(minutes=5)}]

        out = build_metrics(pack, returns, return_items, dispatch, ["X1"], {}, now,
                            pending_return_count=2)

        self.assertEqual(out["packing"]["issues_caught"], 1)
        self.assertEqual(out["returns"]["processed"], 1)
        self.assertEqual(out["returns"]["conditions"], {"Damaged": 1})
        self.assertEqual(out["returns"]["pending_review"], 2)
        self.assertEqual(out["dispatch"]["parcels"], 1)
        self.assertEqual(out["dispatch"]["waiting_parcels"], 1)

    def test_independent_qc_queue_and_line_results_are_visible(self):
        now = datetime(2026, 8, 2, 18, 0)
        qc = [
            {"station": "Line 1", "status": "Passed", "recheck_count": 0,
             "staged_at": now - timedelta(minutes=4), "duration_sec": 120},
            {"station": "Line 2", "status": "Failed", "recheck_count": 1,
             "staged_at": now - timedelta(minutes=8), "duration_sec": 150},
        ]

        out = build_metrics([], [], [], [], [], {"QC Inspector": now.isoformat()},
                            now, line_count=2, qc_rows=qc)

        self.assertEqual(out["packing"]["lines"][0]["qc_passed"], 1)
        self.assertEqual(out["packing"]["lines"][1]["qc_failures"], 1)
        self.assertEqual(out["quality"]["open_holds"], 1)
        self.assertEqual(out["quality"]["overdue_holds"], 1)
        self.assertEqual(out["quality"]["failures_caught"], 1)
        self.assertEqual(out["quality"]["status"], "active")
