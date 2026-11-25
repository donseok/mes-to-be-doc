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

    def tearDown(self):
        self.db.close()

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

    def build(self, ext=None, cfg=None):
        if cfg is not None:
            self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        out = self.tmp / "out"
        rc = ot.main(["build", "--config", str(self.cfg_path), "--extracts", str(ext or self.extract()),
                      "--out", str(out)])
        self.assertEqual(rc, 0)
        return out

    def test_multi_parent_shipment_keeps_all_lots(self):
        out = self.build()
        leaves = {r["leaf_key"] for r in self.read(out / "paths.csv")}
        self.assertNotIn("LOT-T1|C1B", leaves)  # 출하된 LOT는 말단이 아니다
        mmd = (out / "lineage.mmd").read_text(encoding="utf-8")
        self.assertIn("ship_lot_item_LOT_T1_C1B --> shipment_SH_T1", mmd)
        self.assertIn("ship_lot_item_LOT_T1_C1A1 --> shipment_SH_T1", mmd)

    def test_lineage_cycle_and_self_reference(self):
        self.db.execute("UPDATE TODO_COIL SET MOTHER_COIL_NO='C1A1' WHERE COIL_NO='C1'")
        out = self.build()
        checks = [c for c in self.read(out / "checks.csv") if c["check"] == "계보 순환"]
        self.assertTrue(checks)
        paths = {r["leaf_key"]: r["path"] for r in self.read(out / "paths.csv")}
        self.assertTrue(paths["SH-T1"].startswith("주문:ORD-TEST-001"))
        self.assertLess(paths["SH-T1"].count("코일:C1 "), 2)
        self.db.execute("UPDATE TODO_COIL SET MOTHER_COIL_NO='C1' WHERE COIL_NO='C1'")
        out = self.build()
        self.assertTrue(any(c["check"] == "계보 순환" and c["key"] == "C1" for c in self.read(out / "checks.csv")))

    def test_lineage_with_composite_key(self):
        cfg = ansi_config()
        cfg["nodes"]["coil"]["key"] = ["COIL_NO", "WT"]
        for n in ("progress", "judgement", "ship_lot_item", "erp_send"):
            cfg["nodes"][n]["join"] = {"COIL_NO": "COIL_NO"}
        self.cfg = cfg
        out = self.build(cfg=cfg)
        links = self.read(out / "links.csv")
        c1a = [r for r in links if r["node"] == "coil" and r["key"] == "C1A|10.0"][0]
        self.assertEqual((c1a["parent_node"], c1a["parent_key"]), ("coil", "C1|20.0"))
        self.assertFalse([c for c in self.read(out / "checks.csv") if c["node"] == "coil"])

    def test_duplicate_rows_are_reported_once(self):
        ext = self.extract()
        with (ext / "coil.csv").open(encoding="utf-8") as f:
            lines = f.read().splitlines()
        (ext / "coil.csv").write_text("\n".join(lines + lines[1:]) + "\n", encoding="utf-8")
        out = self.build(ext=ext)
        self.assertEqual(len([r for r in self.read(out / "links.csv") if r["node"] == "coil"]), 4)
        self.assertTrue(any(c["check"] == "중복 키" for c in self.read(out / "checks.csv")))

    def test_oracle_lineage_is_wrapped_in_in(self):
        cfg = ot.load_config(TOOL / "trace_path.example.yaml")
        sql = ot.node_set_sql(cfg, "coil")
        self.assertTrue(sql.startswith("SELECT n0.* FROM TODO_COIL n0 WHERE n0.COIL_NO IN (SELECT x0.COIL_NO"))

    def test_custom_sql_with_trailing_comment_and_semicolon(self):
        cfg = ansi_config()
        cfg["nodes"]["order_item"] = {
            "label": "주문품목", "parent": "order", "key": ["ORD_NO", "ORD_SEQ"], "join": {"ORD_NO": "ORD_NO"},
            "custom_sql": "SELECT * FROM TODO_ORDER_ITEM WHERE ORD_NO = :order_no; -- 주문품목", "diagram": True}
        ot.validate_config(cfg)
        sql = ot.node_set_sql(cfg, "mo")
        rows = self.db.execute(sql, {"order_no": "ORD-TEST-001"}).fetchall()
        self.assertEqual([r[0] for r in rows], ["MO-T1"])

    def test_columns_keep_linking_and_event_columns(self):
        cfg = ot.load_config(TOOL / "trace_path.example.yaml")
        cfg["nodes"]["work_order"]["columns"] = ["WO_NO"]
        sql = ot.node_set_sql(cfg, "work_order")
        for c in ("MO_NO", "ISSUE_DT", "PROC_CD", "WO_STAT_CD"):
            self.assertIn(f"n0.{c}", sql)

    def test_mask_on_linking_column_is_rejected(self):
        cfg = ansi_config()
        cfg["mask"]["order"]["ORD_NO"] = "hash"
        with self.assertRaises(ot.ConfigError):
            ot.validate_config(cfg)

    def test_label_cannot_inject_sql(self):
        cfg = ansi_config()
        cfg["nodes"]["mo"]["label"] = "MO)\nDELETE FROM TODO_COIL;\n--"
        with self.assertRaises(ot.ConfigError):
            ot.validate_config(cfg)

    def test_select_only_bypasses_are_rejected(self):
        bad = [
            "SELECT '--' AS x FROM DUAL; DELETE FROM TODO_COIL",
            "SELECT '/*' x FROM DUAL; DROP TABLE T; SELECT '*/' FROM DUAL",
            "SELECT 1 FROM DUAL\n/\nSELECT 2 FROM DUAL",
            "SELECT 1 FROM DUAL\n\nHOST rm -rf ~",
            "RENAME TODO_COIL TO X",
            "SELECT * FROM TODO_COIL@PROD_LINK",
            "SELECT * FROM TODO_COIL FOR UPDATE",
            "SELECT 'x' FROM DUAL WHERE 1=1 /* 닫히지 않음",
        ]
        for sql in bad:
            with self.assertRaises(ot.ConfigError, msg=sql):
                ot.clean_select(sql, "test")
        ok = ot.clean_select("SELECT 'a;b' AS x, q'[it's]' AS y FROM DUAL -- 끝\n", "test")
        self.assertEqual(ok, "SELECT 'a;b' AS x, q'[it's]' AS y FROM DUAL")
        with self.assertRaises(ot.ConfigError):
            ot.clean_select("ORD_NO = :order_no;", "f", fragment=True)

    def test_sql_command_writes_files(self):
        out = self.tmp / "sql"
        rc = ot.main(["sql", "--config", str(TOOL / "trace_path.example.yaml"), "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertTrue((out / "05_coil.sql").exists())
        self.assertTrue((out / "variants" / "split.sql").exists())

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
