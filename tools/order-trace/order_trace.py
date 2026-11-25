#!/usr/bin/env python3
"""주문 번호 추적 도구 (order-trace).

출하가 끝난 주문 한 건을 주문 → MO → 작업지시 → 코일(분할 포함) → 판정 → 출하 → ERP 전기까지
번호로 따라가, 번호 연결표·진도 타임라인·코일 계보도·마스킹 스냅샷을 만든다.

운영 DB에 직접 붙지 않는다.
  1) sql   : 조회 경로 설정(YAML/JSON)에서 SELECT 문만 만든다. DBA가 읽기 전용 복제본에서 실행해 CSV로 내보낸다.
  2) build : 내보낸 CSV로 산출물을 만든다.

표준 라이브러리만 쓴다. YAML 설정은 PyYAML이 있을 때만 읽고, 없으면 JSON 설정을 쓴다.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

SALT_ENV = "ORDER_TRACE_SALT"
FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|EXECUTE|EXEC|BEGIN|CALL|LOCK|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#.]*$")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- 설정

def load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ConfigError("YAML 설정을 읽으려면 PyYAML이 필요하다. 없으면 같은 내용을 JSON으로 저장해 쓴다.") from e
        cfg = yaml.safe_load(text)
    else:
        cfg = json.loads(text)
    validate_config(cfg)
    return cfg


def _check_ident(name: str, where: str) -> None:
    if not isinstance(name, str) or not IDENT.match(name):
        raise ConfigError(f"{where}: 식별자로 쓸 수 없는 값이다: {name!r}")


def validate_config(cfg: dict) -> None:
    if not isinstance(cfg, dict) or "nodes" not in cfg:
        raise ConfigError("설정에 nodes가 없다.")
    nodes = cfg["nodes"]
    dialect = cfg.get("dialect", "oracle")
    if dialect not in ("oracle", "ansi"):
        raise ConfigError("dialect는 oracle 또는 ansi다.")
    roots = [n for n, d in nodes.items() if not d.get("parent")]
    if len(roots) != 1:
        raise ConfigError(f"부모가 없는 노드(시작점)는 하나여야 한다: {roots}")
    for name, d in nodes.items():
        _check_ident(name, "노드 이름")
        if "custom_sql" in d:
            check_select_only(d["custom_sql"], f"{name}.custom_sql")
        else:
            _check_ident(d.get("table", ""), f"{name}.table")
        key = d.get("key")
        if not key or not isinstance(key, list):
            raise ConfigError(f"{name}: key(행을 구별하는 컬럼 목록)가 필요하다.")
        for c in key:
            _check_ident(c, f"{name}.key")
        parent = d.get("parent")
        if parent:
            if parent not in nodes:
                raise ConfigError(f"{name}: 부모 노드 {parent}가 없다.")
            join = d.get("join")
            if not join or not isinstance(join, dict):
                raise ConfigError(f"{name}: join(자기 컬럼: 부모 컬럼)이 필요하다.")
            for c, p in join.items():
                _check_ident(c, f"{name}.join")
                _check_ident(p, f"{name}.join")
        else:
            if "custom_sql" not in d and not d.get("filter"):
                raise ConfigError(f"{name}: 시작 노드에는 filter(예: ORD_NO = :order_no)가 필요하다.")
            if d.get("filter"):
                check_select_only(d["filter"], f"{name}.filter")
        lin = d.get("lineage")
        if lin:
            _check_ident(lin.get("parent_column", ""), f"{name}.lineage.parent_column")
            _check_ident(lin.get("key_column", ""), f"{name}.lineage.key_column")
        for c in d.get("columns", []) or []:
            _check_ident(c, f"{name}.columns")
        if d.get("time_column"):
            _check_ident(d["time_column"], f"{name}.time_column")
    # 순환 검사
    for name in nodes:
        seen, cur = set(), name
        while cur:
            if cur in seen:
                raise ConfigError(f"부모 관계에 순환이 있다: {name}")
            seen.add(cur)
            cur = nodes[cur].get("parent")
    for vname, sql in (cfg.get("variants") or {}).items():
        _check_ident(vname, "variants 이름")
        check_select_only(sql, f"variants.{vname}")
    for node, cols in (cfg.get("mask") or {}).items():
        if node not in nodes:
            raise ConfigError(f"mask: 노드 {node}가 없다.")
        for col, how in cols.items():
            if how not in ("hash", "drop", "redact"):
                raise ConfigError(f"mask.{node}.{col}: hash, drop, redact 중 하나다.")


def check_select_only(sql: str, where: str) -> None:
    body = re.sub(r"--[^\n]*", "", sql)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    if ";" in body.strip().rstrip(";"):
        raise ConfigError(f"{where}: 문장은 하나만 쓴다.")
    m = FORBIDDEN_SQL.search(body)
    if m:
        raise ConfigError(f"{where}: 조회 전용 도구라 {m.group(1).upper()}를 쓸 수 없다.")
    first = body.strip().split(None, 1)[0].upper() if body.strip() else ""
    if where.endswith(".filter"):
        return
    if first not in ("SELECT", "WITH"):
        raise ConfigError(f"{where}: SELECT 또는 WITH로 시작해야 한다.")


def chain_to_root(cfg: dict, name: str) -> list[str]:
    out, cur = [], name
    while cur:
        out.append(cur)
        cur = cfg["nodes"][cur].get("parent")
    return list(reversed(out))


def children_of(cfg: dict) -> dict[str, list[str]]:
    ch = defaultdict(list)
    for n, d in cfg["nodes"].items():
        if d.get("parent"):
            ch[d["parent"]].append(n)
    return ch


def topo_order(cfg: dict) -> list[str]:
    ch = children_of(cfg)
    root = next(n for n, d in cfg["nodes"].items() if not d.get("parent"))
    order, stack = [], [root]
    while stack:
        n = stack.pop(0)
        order.append(n)
        stack.extend(ch.get(n, []))
    return order


# ---------------------------------------------------------------- SQL 생성

def _cols(d: dict, alias: str) -> str:
    cols = d.get("columns")
    if not cols:
        return f"{alias}.*"
    need = list(dict.fromkeys(list(d["key"]) + list(d.get("join", {}).keys()) + cols
                              + ([d["time_column"]] if d.get("time_column") else [])
                              + ([d["lineage"]["parent_column"]] if d.get("lineage") else [])))
    return ", ".join(f"{alias}.{c}" for c in need)


def node_set_sql(cfg: dict, name: str, depth: int = 0, select: str | None = None) -> str:
    """노드 name에서 대상 주문에 속한 행 집합을 고르는 SELECT 문."""
    d = cfg["nodes"][name]
    dialect = cfg.get("dialect", "oracle")
    a = f"n{depth}"
    sel = select or _cols(d, a)
    if "custom_sql" in d:
        inner = d["custom_sql"].strip().rstrip(";")
        return f"SELECT {sel.replace(a + '.', 'c' + str(depth) + '.')} FROM ({inner}) c{depth}" if select else inner
    parent = d.get("parent")
    if not parent:
        base_where = d["filter"]
        base = f"SELECT {sel} FROM {d['table']} {a} WHERE {base_where.replace('{a}', a)}"
        if not d.get("lineage"):
            return base
    else:
        pd_ = cfg["nodes"][parent]
        child_cols = list(d["join"].keys())
        parent_cols = [d["join"][c] for c in child_cols]
        lhs = _tuple([f"{a}.{c}" for c in child_cols])
        sub = node_set_sql(cfg, parent, depth + 1,
                           select=", ".join(f"n{depth + 1}.{c}" for c in parent_cols))
        base_where = f"{lhs} IN ({sub})"
        base = f"SELECT {sel} FROM {d['table']} {a} WHERE {base_where}"
        if not d.get("lineage"):
            return base
    # 계보(분할) 노드: 부모 노드와 이어진 코일에서 시작해 자식 코일을 모두 따라간다.
    lin = d["lineage"]
    kc, pc = lin["key_column"], lin["parent_column"]
    if dialect == "oracle":
        return (f"SELECT {sel} FROM {d['table']} {a} "
                f"START WITH {base_where} "
                f"CONNECT BY NOCYCLE PRIOR {a}.{kc} = {a}.{pc}")
    # ansi: 재귀 CTE. 시작 집합의 키에서 자식 키를 모은 뒤 원 테이블과 다시 잇는다.
    t = d["table"]
    r = f"r{depth}"
    return (f"SELECT {sel} FROM {t} {a} WHERE {a}.{kc} IN ("
            f"WITH RECURSIVE {r}(k) AS ("
            f"SELECT {a}.{kc} FROM {t} {a} WHERE {base_where} "
            f"UNION SELECT x{depth}.{kc} FROM {t} x{depth} JOIN {r} ON x{depth}.{pc} = {r}.k"
            f") SELECT k FROM {r})")


def _tuple(items: list[str]) -> str:
    return items[0] if len(items) == 1 else "(" + ", ".join(items) + ")"


def cmd_sql(args) -> int:
    cfg = load_config(Path(args.config))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    todo = []
    order = topo_order(cfg)
    for i, name in enumerate(order, 1):
        d = cfg["nodes"][name]
        sql = node_set_sql(cfg, name)
        if "TODO" in json.dumps(d, ensure_ascii=False):
            todo.append(name)
        header = (f"-- order-trace {name} ({d.get('label', name)})\n"
                  f"-- 읽기 전용 복제본에서 실행하고 결과를 extracts/{name}.csv 로 내보낸다(머리글 포함, UTF-8).\n"
                  f"-- 바인드 변수 :order_no 에 추적할 주문번호를 넣는다. 날짜는 YYYY-MM-DD HH24:MI:SS 형식으로 내보낸다.\n")
        (out / f"{i:02d}_{name}.sql").write_text(header + sql + "\n", encoding="utf-8")
    vdir = out / "variants"
    for vname, vsql in (cfg.get("variants") or {}).items():
        vdir.mkdir(exist_ok=True)
        (vdir / f"{vname}.sql").write_text(
            f"-- 변형 주문 후보 선정: {vname}. 결과에서 한 건을 골라 :order_no 로 쓴다.\n"
            + vsql.strip().rstrip(";") + "\n", encoding="utf-8")
    print(f"SQL {len(order)}개를 {out} 에 만들었다. 실행 순서는 파일 번호 순이다.")
    if todo:
        print("아직 TODO가 남은 노드: " + ", ".join(todo) + " (실제 테이블·컬럼명으로 바꾼 뒤 실행한다)")
    return 0


# ---------------------------------------------------------------- build

def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        rows = [{(k or "").strip().upper(): (v if v is not None else "") for k, v in row.items()} for row in r]
        header = [(h or "").strip().upper() for h in (r.fieldnames or [])]
    return header, rows


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def masker(cfg: dict):
    rules = {n: {c.upper(): how for c, how in cols.items()} for n, cols in (cfg.get("mask") or {}).items()}
    needs_salt = any(how == "hash" for cols in rules.values() for how in cols.values())
    salt = os.environ.get(SALT_ENV, "")
    if needs_salt and not salt:
        raise ConfigError(f"hash 마스킹을 쓰려면 환경변수 {SALT_ENV}에 비밀값을 넣는다(파일이나 git에는 두지 않는다).")

    def apply(node: str, header: list[str], rows: list[dict]):
        r = rules.get(node, {})
        new_header = [h for h in header if r.get(h) != "drop"]
        out = []
        for row in rows:
            nr = {}
            for h in new_header:
                v = row.get(h, "")
                how = r.get(h)
                if how == "hash" and v != "":
                    v = "H:" + hmac.new(salt.encode(), v.encode(), hashlib.sha256).hexdigest()[:12]
                elif how == "redact" and v != "":
                    v = "***"
                nr[h] = v
            out.append(nr)
        return new_header, out

    return apply


def key_of(row: dict, cols: list[str]) -> str:
    return "|".join(row.get(c.upper(), "") for c in cols)


def mermaid_id(node: str, key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", f"{node}_{key}")


def cmd_build(args) -> int:
    cfg = load_config(Path(args.config))
    ext, out = Path(args.extracts), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mask = masker(cfg)
    nodes = cfg["nodes"]
    order = topo_order(cfg)
    data: dict[str, list[dict]] = {}
    manifest = {"run_at": args.run_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "config_sha256": sha256_file(Path(args.config)), "inputs": {}}
    missing = []
    for name in order:
        p = ext / f"{name}.csv"
        if not p.exists():
            missing.append(name)
            data[name] = []
            continue
        header, rows = read_csv(p)
        manifest["inputs"][name] = {"file": p.name, "sha256": sha256_file(p), "rows": len(rows)}
        mh, mrows = mask(name, header, rows)
        write_csv(out / "snapshot" / f"{name}.csv", mh, mrows)
        data[name] = mrows

    # 번호 연결표와 정합성 검사
    links, checks = [], []
    index = {}  # (node, key) -> row
    for name in order:
        d = nodes[name]
        for row in data[name]:
            index[(name, key_of(row, d["key"]))] = row
    parent_of: dict[tuple, tuple | None] = {}
    for name in order:
        d = nodes[name]
        parent = d.get("parent")
        lin = d.get("lineage")
        for row in data[name]:
            k = key_of(row, d["key"])
            pk_node, pk = None, None
            if lin and row.get(lin["parent_column"].upper()):
                cand = (name, row.get(lin["parent_column"].upper()))
                if len(d["key"]) == 1 and cand in index:
                    pk_node, pk = cand
            if pk_node is None and parent:
                pd_ = nodes[parent]
                inv = {v.upper(): c.upper() for c, v in d["join"].items()}
                pvals = [row.get(inv.get(pc.upper(), ""), "") for pc in pd_["key"]]
                if all(pc.upper() in inv for pc in pd_["key"]):
                    pk_node, pk = parent, "|".join(pvals)
                    if (pk_node, pk) not in index:
                        checks.append({"check": "부모 없음", "node": name, "key": k,
                                       "detail": f"{parent} {pk} 를 추출본에서 찾지 못했다"})
                else:
                    # join이 부모 키 전체를 덮지 않으면 부모 행 하나로 정할 수 없다. 조인 값으로 찾는다.
                    hits = sorted((pr for pr in data[parent]
                                   if all(pr.get(v.upper(), "") == row.get(c.upper(), "") for c, v in d["join"].items())),
                                  key=lambda pr: key_of(pr, pd_["key"]))
                    if hits:
                        # 출하처럼 여러 코일이 한 건에 모이는 경우다. 연결은 모두 남기고 경로표에는 첫 부모를 쓴다.
                        pk_node, pk = parent, key_of(hits[0], pd_["key"])
                        for extra in hits[1:]:
                            links.append({"node": name, "label": d.get("label", name), "key": k,
                                          "parent_node": parent, "parent_key": key_of(extra, pd_["key"])})
                    else:
                        checks.append({"check": "부모 없음", "node": name, "key": k,
                                       "detail": f"{parent} 에서 조인 값이 같은 행을 찾지 못했다"})
            parent_of[(name, k)] = (pk_node, pk) if pk_node else None
            links.append({"node": name, "label": d.get("label", name), "key": k,
                          "parent_node": pk_node or "", "parent_key": pk or ""})
    write_csv(out / "links.csv", ["node", "label", "key", "parent_node", "parent_key"], links)

    # 경로표: 자식이 없는 행마다 시작점까지의 번호 경로
    has_child = {v for v in parent_of.values() if v}
    paths = []
    for (name, k), p in parent_of.items():
        if (name, k) in has_child:
            continue
        chain, cur, guard = [], (name, k), 0
        while cur and guard < 200:
            chain.append(f"{nodes[cur[0]].get('label', cur[0])}:{cur[1]}")
            cur, guard = parent_of.get(cur), guard + 1
        paths.append({"leaf_node": name, "leaf_key": k, "path": " > ".join(reversed(chain))})
    write_csv(out / "paths.csv", ["leaf_node", "leaf_key", "path"], paths)

    # 진도 타임라인
    timeline = []
    for name in order:
        d = nodes[name]
        tc = d.get("time_column")
        if not tc:
            continue
        evcols = [c.upper() for c in d.get("event_columns", [])]
        for row in data[name]:
            t = row.get(tc.upper(), "")
            if not t:
                checks.append({"check": "시각 없음", "node": name, "key": key_of(row, d["key"]), "detail": tc})
            timeline.append({"time": t, "node": name, "label": d.get("label", name),
                             "key": key_of(row, d["key"]),
                             "parent": "|".join(parent_of.get((name, key_of(row, d["key"]))) or ("", "")),
                             "event": "; ".join(f"{c}={row.get(c, '')}" for c in evcols)})
    timeline.sort(key=lambda r: (r["time"] == "", r["time"], r["node"], r["key"]))
    write_csv(out / "timeline.csv", ["time", "node", "label", "key", "parent", "event"], timeline)

    # 계보도(Mermaid): diagram: true 인 노드만 그린다.
    shown = {n for n in order if nodes[n].get("diagram")}
    lines = ["flowchart LR"]
    for (name, k), p in parent_of.items():
        if name not in shown:
            continue
        lines.append(f'  {mermaid_id(name, k)}["{nodes[name].get("label", name)}<br/>{k}"]')
    for (name, k), p in parent_of.items():
        if name in shown and p and p[0] in shown:
            lines.append(f"  {mermaid_id(p[0], p[1])} --> {mermaid_id(name, k)}")
    (out / "lineage.mmd").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for name in missing:
        checks.append({"check": "추출본 없음", "node": name, "key": "", "detail": f"{name}.csv"})
    for name in order:
        if name not in missing and not data[name]:
            checks.append({"check": "행 없음", "node": name, "key": "", "detail": "조회 결과가 비었다"})
    write_csv(out / "checks.csv", ["check", "node", "key", "detail"], checks)
    manifest["outputs"] = {"links": len(links), "paths": len(paths), "timeline": len(timeline), "checks": len(checks)}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"산출물을 {out} 에 만들었다: 연결 {len(links)}행, 경로 {len(paths)}행, 타임라인 {len(timeline)}행, 검사 {len(checks)}건")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="주문 번호 추적 도구 (order-trace)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sql", help="조회 경로 설정에서 SELECT 문을 만든다")
    s.add_argument("--config", required=True)
    s.add_argument("--out", default="sql")
    b = sub.add_parser("build", help="내보낸 CSV로 산출물을 만든다")
    b.add_argument("--config", required=True)
    b.add_argument("--extracts", default="extracts")
    b.add_argument("--out", default="out")
    b.add_argument("--run-at", help="manifest에 적을 실행 시각(기본: 현재 UTC)")
    c = sub.add_parser("check", help="설정만 검사한다")
    c.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "sql":
            return cmd_sql(args)
        if args.cmd == "build":
            return cmd_build(args)
        load_config(Path(args.config))
        print("설정에 문제가 없다.")
        return 0
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
