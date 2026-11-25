"""order-trace 시험. 예시 설정을 SQLite(ansi)로 실행해 SQL 생성부터 산출물까지 확인한다.

시험 데이터는 모두 지어낸 값이다(ORD-TEST-001 등). 실제 주문·고객 정보가 아니다.
실행: python -m unittest discover -s tools/order-trace/tests
"""
import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
sys.path.insert(0, str(TOOL))
import order_trace as ot  # noqa: E402

SCHEMA = """
CREATE TABLE TODO_ORDER_HDR (ORD_NO TEXT, CUST_NM TEXT, UNIT_PRICE REAL, ORD_STAT_CD TEXT, LINE_CD TEXT);
CREATE TABLE TODO_ORDER_ITEM (ORD_NO TEXT, ORD_SEQ INTEGER, SPEC TEXT);
CREATE TABLE TODO_MO (MO_NO TEXT, ORD_NO TEXT, ORD_SEQ INTEGER);
CREATE TABLE TODO_WORK_ORDER (WO_NO TEXT, MO_NO TEXT, ISSUE_DT TEXT, PROC_CD TEXT, WO_STAT_CD TEXT);
CREATE TABLE TODO_COIL (COIL_NO TEXT, MO_NO TEXT, MOTHER_COIL_NO TEXT, WT REAL);
CREATE TABLE TODO_COIL_PRGS_HIST (COIL_NO TEXT, HIST_SEQ INTEGER, CHG_DT TEXT, PRGS_CD TEXT, WORK_STAT_CD TEXT, PROC_CD TEXT);
CREATE TABLE TODO_QLTY_JDG (COIL_NO TEXT, JDG_SEQ INTEGER, JDG_DT TEXT, JDG_CD TEXT, GRADE_CD TEXT);
CREATE TABLE TODO_SHIP_LOT_ITEM (LOT_NO TEXT, COIL_NO TEXT);
CREATE TABLE TODO_SHIPMENT (SHIP_NO TEXT, LOT_NO TEXT, SHIP_DT TEXT, SHIP_STAT_CD TEXT, CAR_NO TEXT, DRIVER_NM TEXT, DRIVER_TEL TEXT);
CREATE TABLE TODO_IF_ERP_PRDN (IF_SEQ INTEGER, COIL_NO TEXT, SEND_DT TEXT, IF_ID TEXT, EVENT_CD TEXT, SEND_STAT TEXT);
"""

ROWS = {
    "TODO_ORDER_HDR": [("ORD-TEST-001", "가상고객A", 1000.0, "SHIPPED", "CGL"),
                       ("ORD-TEST-999", "가상고객B", 2000.0, "SHIPPED", "CCL")],
    "TODO_ORDER_ITEM": [("ORD-TEST-001", 1, "0.5x1219"), ("ORD-TEST-999", 1, "0.4x914")],
    "TODO_MO": [("MO-T1", "ORD-TEST-001", 1), ("MO-T9", "ORD-TEST-999", 1)],
    "TODO_WORK_ORDER": [("WO-T1", "MO-T1", "2026-10-01 08:00:00", "PCM", "DONE"),
                        ("WO-T2", "MO-T1", "2026-10-02 09:00:00", "CGL", "DONE")],
    # C1 → C1A, C1B 분할, C1A → C1A1 재분할. C9 는 다른 주문이라 나오면 안 된다.
    "TODO_COIL": [("C1", "MO-T1", None, 20.0), ("C1A", None, "C1", 10.0), ("C1B", None, "C1", 10.0),
                  ("C1A1", None, "C1A", 9.5), ("C9", "MO-T9", None, 15.0)],
    "TODO_COIL_PRGS_HIST": [("C1", 1, "2026-10-01 08:10:00", "A1", "W", "PCM"),
                            ("C1A", 1, "2026-10-02 09:30:00", "B1", "W", "CGL"),
                            ("C1A1", 1, "2026-10-03 10:00:00", "K1", "C", "PACK"),
                            ("C9", 1, "2026-10-01 07:00:00", "A1", "W", "PCM")],
    "TODO_QLTY_JDG": [("C1A", 1, "2026-10-02 12:00:00", "NG", "B"),
                      ("C1A", 2, "2026-10-02 15:00:00", "OK", "A")],
    "TODO_SHIP_LOT_ITEM": [("LOT-T1", "C1A1"), ("LOT-T1", "C1B"), ("LOT-T9", "C9")],
    "TODO_SHIPMENT": [("SH-T1", "LOT-T1", "2026-10-05 14:00:00", "OUT", "00가0000", "가상기사", "010-0000-0000")],
    "TODO_IF_ERP_PRDN": [(1, "C1A1", "2026-10-03 10:05:00", "B47S3004", "PACK", "S"),
                         (2, "C9", "2026-10-01 07:05:00", "B47S3004", "PRDN", "S")],
}


def ansi_config():
    cfg = ot.load_config(TOOL / "trace_path.example.yaml")
    cfg["dialect"] = "ansi"
    return cfg


class OrderTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        for t, rows in ROWS.items():
            ph = ",".join("?" * len(rows[0]))
            self.db.executemany(f"INSERT INTO {t} VALUES ({ph})", rows)
        os.environ[ot.SALT_ENV] = "test-only-salt"
        self.cfg = ansi_config()
        self.cfg_path = self.tmp / "cfg.json"
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")

    def extract(self):
        ext = self.tmp / "extracts"
        ext.mkdir(exist_ok=True)
        for name in ot.topo_order(self.cfg):
            sql = ot.node_set_sql(self.cfg, name)
            cur = self.db.execute(sql, {"order_no": "ORD-TEST-001"})
            header = [c[0] for c in cur.description]
            with (ext / f"{name}.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(["" if v is None else v for v in r] for r in cur.fetchall())
        return ext

    def read(self, path):
        with path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_sql_follows_split_lineage_and_excludes_other_orders(self):
        sql = ot.node_set_sql(self.cfg, "coil")
        coils = {r[0] for r in self.db.execute(sql, {"order_no": "ORD-TEST-001"}).fetchall()}
        self.assertEqual(coils, {"C1", "C1A", "C1B", "C1A1"})

    def test_oracle_sql_uses_connect_by(self):
        cfg = ot.load_config(TOOL / "trace_path.example.yaml")
        sql = ot.node_set_sql(cfg, "progress")
        self.assertIn("CONNECT BY NOCYCLE PRIOR", sql)
        self.assertIn(":order_no", sql)

    def test_build_outputs(self):
        ext = self.extract()
        out = self.tmp / "out"
        rc = ot.main(["build", "--config", str(self.cfg_path), "--extracts", str(ext),
                      "--out", str(out), "--run-at", "2026-11-25T00:00:00Z"])
        self.assertEqual(rc, 0)
        paths = {r["leaf_key"]: r["path"] for r in self.read(out / "paths.csv")}
        self.assertEqual(paths["SH-T1"],
                         "주문:ORD-TEST-001 > 주문품목:ORD-TEST-001|1 > MO:MO-T1 > 코일:C1 > 코일:C1A > 코일:C1A1"
                         " > 출하 LOT:LOT-T1|C1A1 > 출하:SH-T1")
        links = self.read(out / "links.csv")
        ship_parents = {r["parent_key"] for r in links if r["node"] == "shipment"}
        self.assertEqual(ship_parents, {"LOT-T1|C1A1", "LOT-T1|C1B"})
        times = [r["time"] for r in self.read(out / "timeline.csv")]
        self.assertEqual(times, sorted(times))
        self.assertNotIn("C9", json.dumps(self.read(out / "links.csv")))
        mmd = (out / "lineage.mmd").read_text(encoding="utf-8")
        self.assertIn("coil_C1 --> coil_C1A", mmd)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["inputs"]["coil"]["rows"], 4)

    def test_masking(self):
        ext = self.extract()
        out = self.tmp / "out"
        ot.main(["build", "--config", str(self.cfg_path), "--extracts", str(ext), "--out", str(out)])
        ship = self.read(out / "snapshot" / "shipment.csv")[0]
        self.assertTrue(ship["CAR_NO"].startswith("H:"))
        self.assertNotIn("DRIVER_TEL", ship)
        order = self.read(out / "snapshot" / "order.csv")[0]
        self.assertTrue(order["CUST_NM"].startswith("H:"))
        self.assertNotIn("UNIT_PRICE", order)

    def test_hash_requires_salt(self):
        del os.environ[ot.SALT_ENV]
        ext = self.extract()
        rc = ot.main(["build", "--config", str(self.cfg_path), "--extracts", str(ext), "--out", str(self.tmp / "o")])
        self.assertEqual(rc, 2)

    def test_rejects_non_select(self):
        bad = dict(self.cfg)
        bad["variants"] = {"x": "DELETE FROM TODO_COIL"}
        with self.assertRaises(ot.ConfigError):
            ot.validate_config(bad)
        bad["variants"] = {"x": "SELECT 1 FROM DUAL; DROP TABLE TODO_COIL"}
        with self.assertRaises(ot.ConfigError):
            ot.validate_config(bad)

    def test_missing_parent_is_reported(self):
        ext = self.extract()
        with (ext / "judgement.csv").open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(["CX", "1", "2026-10-02 12:00:00", "OK", "A"])
        out = self.tmp / "out"
        ot.main(["build", "--config", str(self.cfg_path), "--extracts", str(ext), "--out", str(out)])
        checks = self.read(out / "checks.csv")
        self.assertTrue(any(c["check"] == "부모 없음" and c["node"] == "judgement" for c in checks))


if __name__ == "__main__":
    unittest.main()
