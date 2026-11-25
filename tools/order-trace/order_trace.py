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
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
SALT_ENV = "ORDER_TRACE_SALT"
FORBIDDEN_WORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE", "CREATE",
    "EXECUTE", "EXEC", "BEGIN", "DECLARE", "CALL", "LOCK", "COMMIT", "ROLLBACK", "SAVEPOINT", "RENAME",
    "FLASHBACK", "COMMENT", "PURGE", "AUDIT", "NOAUDIT", "ANALYZE",
}
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#.]*$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- SQL 검사

def tokenize_sql(sql: str) -> tuple[str, str]:
    """(주석을 뺀 원문, 리터럴·따옴표 식별자까지 가린 코드)를 돌려준다.

    작은따옴표 리터럴('' 이스케이프 포함), Oracle q'[...]' 리터럴, 큰따옴표 식별자,
    -- 행 주석, /* */ 블록 주석을 구분한다. 닫히지 않은 리터럴이나 주석은 오류다.
    """
    out_src, out_code, i, n = [], [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out_src.append(" ")
            out_code.append(" ")
            i = j
        elif c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise ConfigError("닫히지 않은 /* 주석이 있다.")
            out_src.append(" ")
            out_code.append(" ")
            i = j + 2
        elif (c in "qQ" and sql[i + 1:i + 2] == "'") or (c in "nN" and sql[i + 1:i + 3].lower() == "q'"):
            start = i
            if c in "nN":
                i += 1
            if i + 2 >= n:
                raise ConfigError("닫히지 않은 q'...' 리터럴이 있다.")
            delim = sql[i + 2]
            close = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(delim, delim)
            j = sql.find(close + "'", i + 3)
            if j < 0:
                raise ConfigError("닫히지 않은 q'...' 리터럴이 있다.")
            out_src.append(sql[start:j + 2])
            out_code.append(" ?L ")
            i = j + 2
        elif c == "'":
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j < 0:
                    raise ConfigError("닫히지 않은 문자열 리터럴이 있다.")
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            out_src.append(sql[i:j + 1])
            out_code.append(" ?L ")
            i = j + 1
        elif c == '"':
            j = sql.find('"', i + 1)
            if j < 0:
                raise ConfigError("닫히지 않은 큰따옴표 식별자가 있다.")
            out_src.append(sql[i:j + 1])
            out_code.append(" ?Q ")
            i = j + 1
        else:
            out_src.append(c)
            out_code.append(c)
            i += 1
    return "".join(out_src), "".join(out_code)


def clean_select(sql: str, where: str, fragment: bool = False) -> str:
    """조회 전용인지 검사하고, 다른 문장에 끼워 넣을 수 있게 주석과 끝 세미콜론을 뺀 본문을 돌려준다.

    fragment=True 는 WHERE 조건(filter)처럼 SELECT로 시작하지 않는 조각이다.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ConfigError(f"{where}: 비어 있다.")
    # SQL*Plus는 빈 줄에서 문장을 끊고, 다음 줄을 HOST 같은 클라이언트 명령으로 실행할 수 있다.
    if any(not line.strip() for line in sql.strip().splitlines()):
        raise ConfigError(f"{where}: 문장 안에 빈 줄을 두지 않는다(SQL*Plus가 거기서 문장을 끊는다).")
    src, code = tokenize_sql(sql)
    for line in code.splitlines():
        s = line.strip()
        if s and s[0] in "/@!":
            raise ConfigError(f"{where}: 줄 맨 앞의 {s[0]} 는 SQL*Plus 명령으로 실행될 수 있어 쓸 수 없다.")
    body_code = code.strip()
    body_src = src.strip()
    if body_code.endswith(";"):
        if fragment:
            raise ConfigError(f"{where}: 조건에는 세미콜론을 쓰지 않는다.")
        body_code = body_code[:-1].rstrip()
        body_src = body_src[:body_src.rstrip().rfind(";")].rstrip()
    if ";" in body_code:
        raise ConfigError(f"{where}: 문장은 하나만 쓴다.")
    if "@" in body_code:
        raise ConfigError(f"{where}: DB 링크(@)는 쓸 수 없다.")
    words = {w.upper() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_$#]*", body_code)}
    bad = sorted(words & FORBIDDEN_WORDS)
    if bad:
        raise ConfigError(f"{where}: 조회 전용 도구라 {', '.join(bad)} 를 쓸 수 없다.")
    if not fragment:
        first = body_code.split(None, 1)[0].upper().lstrip("(")
        if first not in ("SELECT", "WITH"):
            raise ConfigError(f"{where}: SELECT 또는 WITH로 시작해야 한다.")
    return body_src


def check_output_sql(text: str, where: str) -> None:
    """파일에 쓰기 직전 최종 검사. 머리 주석을 포함한 전체가 조회 문장 하나여야 한다."""
    clean_select(text, where)


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


def _check_ident(name, where: str) -> None:
    if not isinstance(name, str) or not IDENT.match(name):
        raise ConfigError(f"{where}: 식별자로 쓸 수 없는 값이다: {name!r}")


def _check_label(text, where: str) -> None:
    if not isinstance(text, str) or CONTROL.search(text) or any(ch in text for ch in '"<>`'):
        raise ConfigError(f"{where}: 라벨에는 줄바꿈, 제어문자, 따옴표, <, >, ` 를 쓸 수 없다.")


def validate_config(cfg: dict) -> None:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("nodes"), dict) or not cfg["nodes"]:
        raise ConfigError("설정에 nodes가 없다.")
    nodes = cfg["nodes"]
    if cfg.get("dialect", "oracle") not in ("oracle", "ansi"):
        raise ConfigError("dialect는 oracle 또는 ansi다.")
    roots = [n for n, d in nodes.items() if not d.get("parent")]
    if len(roots) != 1:
        raise ConfigError(f"부모가 없는 노드(시작점)는 하나여야 한다: {roots}")
    for name, d in nodes.items():
        _check_ident(name, "노드 이름")
        if "label" in d:
            _check_label(d["label"], f"{name}.label")
        if "custom_sql" in d:
            d["_custom"] = clean_select(d["custom_sql"], f"{name}.custom_sql")
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
        elif "custom_sql" not in d:
            if not d.get("filter"):
                raise ConfigError(f"{name}: 시작 노드에는 filter(예: ORD_NO = :order_no)가 필요하다.")
            d["_filter"] = clean_select(d["filter"], f"{name}.filter", fragment=True)
        lin = d.get("lineage")
        if lin:
            _check_ident(lin.get("parent_column", ""), f"{name}.lineage.parent_column")
            _check_ident(lin.get("key_column", ""), f"{name}.lineage.key_column")
        for field in ("columns", "event_columns"):
            for c in d.get(field) or []:
                _check_ident(c, f"{name}.{field}")
        if d.get("time_column"):
            _check_ident(d["time_column"], f"{name}.time_column")
    for name in nodes:
        seen, cur = set(), name
        while cur:
            if cur in seen:
                raise ConfigError(f"부모 관계에 순환이 있다: {name}")
            seen.add(cur)
            cur = nodes[cur].get("parent")
    variants = cfg.get("variants") or {}
    cfg["_variants"] = {}
    for vname, sql in variants.items():
        _check_ident(vname, "variants 이름")
        cfg["_variants"][vname] = clean_select(sql, f"variants.{vname}")
    for node, cols in (cfg.get("mask") or {}).items():
        if node not in nodes:
            raise ConfigError(f"mask: 노드 {node}가 없다.")
        linked = {c.upper() for c in linking_columns(cfg, node)}
        for col, how in cols.items():
            _check_ident(col, f"mask.{node}")
            if how not in ("hash", "drop", "redact"):
                raise ConfigError(f"mask.{node}.{col}: hash, drop, redact 중 하나다.")
            if col.upper() in linked:
                raise ConfigError(f"mask.{node}.{col}: 연결에 쓰는 컬럼(key, join, lineage, time_column)은 가릴 수 없다. "
                                  "가려야 하는 값이면 그 테이블을 다른 키로 잇는다.")


def linking_columns(cfg: dict, name: str) -> list[str]:
    """이 노드에서 연결·정렬에 쓰는 컬럼. 자식 노드가 조인하는 부모 쪽 컬럼도 포함한다."""
    d = cfg["nodes"][name]
    cols = list(d["key"]) + list((d.get("join") or {}).keys())
    if d.get("lineage"):
        cols += [d["lineage"]["key_column"], d["lineage"]["parent_column"]]
    if d.get("time_column"):
        cols.append(d["time_column"])
    for child in children_of(cfg).get(name, []):
        cols += list(cfg["nodes"][child]["join"].values())
    return list(dict.fromkeys(cols))


def children_of(cfg: dict) -> dict[str, list[str]]:
    ch = defaultdict(list)
    for n, d in cfg["nodes"].items():
        if d.get("parent"):
            ch[d["parent"]].append(n)
    return ch


def topo_order(cfg: dict) -> list[str]:
    ch = children_of(cfg)
    root = next(n for n, d in cfg["nodes"].items() if not d.get("parent"))
    order, queue = [], [root]
    while queue:
        n = queue.pop(0)
        order.append(n)
        queue.extend(ch.get(n, []))
    return order


# ---------------------------------------------------------------- SQL 생성

def _select_list(cfg: dict, name: str, alias: str) -> str:
    d = cfg["nodes"][name]
    if not d.get("columns"):
        return f"{alias}.*"
    need = linking_columns(cfg, name) + list(d.get("event_columns") or []) + list(d["columns"])
    return ", ".join(f"{alias}.{c}" for c in dict.fromkeys(need))


def _tuple(items: list[str]) -> str:
    return items[0] if len(items) == 1 else "(" + ", ".join(items) + ")"


def _base_where(cfg: dict, name: str, alias: str, depth: int) -> str | None:
    """lineage를 따라가기 전, 대상 주문에 직접 이어진 행을 고르는 조건. 시작 노드이면 filter."""
    d = cfg["nodes"][name]
    parent = d.get("parent")
    if not parent:
        return d["_filter"]
    child_cols = list(d["join"].keys())
    parent_cols = [d["join"][c] for c in child_cols]
    sub = node_set_sql(cfg, parent, depth + 1, cols=parent_cols)
    return f"{_tuple([f'{alias}.{c}' for c in child_cols])} IN ({sub})"


def node_set_sql(cfg: dict, name: str, depth: int = 0, cols: list[str] | None = None) -> str:
    """노드 name에서 대상 주문에 속한 행 집합을 고르는 SELECT 문.

    cols 가 있으면 그 컬럼만 고른다(부모 노드를 IN 하위질의로 쓸 때).
    """
    d = cfg["nodes"][name]
    a = f"n{depth}"
    sel = ", ".join(f"{a}.{c}" for c in cols) if cols else _select_list(cfg, name, a)
    if "_custom" in d:
        # 행 주석이 닫는 괄호를 먹지 않도록 주석을 뺀 본문을 쓰고, 줄을 바꿔 닫는다.
        return f"SELECT {sel} FROM (\n{d['_custom']}\n) {a}"
    lin = d.get("lineage")
    if not lin:
        return f"SELECT {sel} FROM {d['table']} {a} WHERE {_base_where(cfg, name, a, depth)}"
    t, kc, pc = d["table"], lin["key_column"], lin["parent_column"]
    x = f"x{depth}"
    start = _base_where(cfg, name, x, depth)
    if cfg.get("dialect", "oracle") == "oracle":
        # 자코일도 시작 조건을 만족하면 CONNECT BY가 같은 행을 여러 번 낸다. IN으로 한 번 감싸 중복을 없앤다.
        inner = (f"SELECT {x}.{kc} FROM {t} {x} START WITH {start} "
                 f"CONNECT BY NOCYCLE PRIOR {x}.{kc} = {x}.{pc}")
    else:
        r = f"r{depth}"
        inner = (f"WITH RECURSIVE {r}(k) AS ("
                 f"SELECT {x}.{kc} FROM {t} {x} WHERE {start} "
                 f"UNION SELECT y{depth}.{kc} FROM {t} y{depth} JOIN {r} ON y{depth}.{pc} = {r}.k"
                 f") SELECT k FROM {r}")
    return f"SELECT {sel} FROM {t} {a} WHERE {a}.{kc} IN ({inner})"


def warn_if_not_ignored(path: Path) -> None:
    try:
        top = subprocess.run(["git", "-C", str(path.parent if not path.exists() else path), "rev-parse",
                              "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return
        r = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q", str(path.resolve())],
                           capture_output=True, timeout=10)
        if r.returncode == 1:
            print(f"경고: {path} 는 git이 무시하지 않는 경로다. 실제 테이블명이나 데이터가 커밋되지 않게 주의한다.",
                  file=sys.stderr)
    except (OSError, subprocess.SubprocessError):
        pass


def cmd_sql(args) -> int:
    cfg = load_config(Path(args.config))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warn_if_not_ignored(out)
    todo = []
    order = topo_order(cfg)
    files = {}
    for i, name in enumerate(order, 1):
        d = cfg["nodes"][name]
        if "TODO" in json.dumps({k: v for k, v in d.items() if not k.startswith("_")}, ensure_ascii=False):
            todo.append(name)
        text = (f"-- order-trace {name} ({d.get('label', name)})\n"
                f"-- 읽기 전용 복제본에서 실행하고 결과를 extracts/{name}.csv 로 내보낸다(머리글 포함, UTF-8).\n"
                f"-- 바인드 변수 :order_no 에 추적할 주문번호를 넣는다. 날짜는 YYYY-MM-DD HH24:MI:SS 형식으로 내보낸다.\n"
                + node_set_sql(cfg, name) + "\n")
        files[out / f"{i:02d}_{name}.sql"] = text
    for vname, vsql in cfg["_variants"].items():
        text = f"-- 변형 주문 후보 선정: {vname}. 결과에서 한 건을 골라 :order_no 로 쓴다.\n{vsql}\n"
        files[out / "variants" / f"{vname}.sql"] = text
    for path, text in files.items():
        check_output_sql(text, path.name)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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


def _mermaid_text(text: str) -> str:
    return text.replace('"', "'").replace("<", "(").replace(">", ")")


def resolve_parents(cfg: dict, order: list[str], data: dict[str, list[dict]], checks: list[dict]):
    """(node, key) → 부모 목록. 첫 항목이 경로표에 쓰는 대표 부모다."""
    nodes = cfg["nodes"]
    index = {(n, key_of(r, nodes[n]["key"])) for n in order for r in data[n]}
    parents: dict[tuple, list[tuple]] = {}
    for name in order:
        d = nodes[name]
        parent = d.get("parent")
        lin = d.get("lineage")
        # 계보 색인: lineage.key_column 값 → 그 행의 키. key가 여러 컬럼이거나 key_column과 달라도 잇는다.
        lin_idx: dict[str, list[str]] = defaultdict(list)
        lin_parent: dict[str, str] = {}
        if lin:
            kc, pc = lin["key_column"].upper(), lin["parent_column"].upper()
            for row in data[name]:
                lin_idx[row.get(kc, "")].append(key_of(row, d["key"]))
            for v, ks in lin_idx.items():
                if v and len(set(ks)) > 1:
                    checks.append({"check": "계보 키 중복", "node": name, "key": v,
                                   "detail": f"{lin['key_column']} 값이 같은 행이 {len(set(ks))}건"})
            for row in data[name]:
                k, own, pv = key_of(row, d["key"]), row.get(kc, ""), row.get(pc, "")
                if not pv:
                    continue
                if pv == own:
                    checks.append({"check": "계보 순환", "node": name, "key": k, "detail": "자기 자신을 부모로 가리킨다"})
                    continue
                if pv in lin_idx:
                    lin_parent[k] = lin_idx[pv][0]
        join_parents = {key_of(row, d["key"]): _join_parents(nodes, name, row, data, index) for row in data[name]}
        if lin:
            # 순환을 찾아 끊는다. 고리 안에서 부모 노드(MO 등)와 직접 이어지는 행의 계보 연결만 버린다.
            # 그런 행이 없으면 고리 전체의 계보 연결을 버린다.
            for start in list(lin_parent):
                path, cur = [], start
                while cur in lin_parent and cur not in path:
                    path.append(cur)
                    cur = lin_parent[cur]
                if cur in path:
                    loop = path[path.index(cur):]
                    cut = [k for k in loop if join_parents.get(k)] or loop
                    for k in cut:
                        lin_parent.pop(k, None)
                    checks.append({"check": "계보 순환", "node": name, "key": ", ".join(cut),
                                   "detail": " > ".join(loop) + " > " + loop[0] + " (끊은 행의 계보 연결을 버렸다)"})
        for row in data[name]:
            k = key_of(row, d["key"])
            if k in lin_parent:
                parents[(name, k)] = [(name, lin_parent[k])]
                continue
            parents[(name, k)] = join_parents[k]
            if parent and not join_parents[k]:
                checks.append({"check": "부모 없음", "node": name, "key": k,
                               "detail": f"{parent} 에서 조인 값이 같은 행을 찾지 못했다"})
    return parents


def _join_parents(nodes: dict, name: str, row: dict, data: dict, index: set) -> list[tuple]:
    d = nodes[name]
    parent = d.get("parent")
    if not parent:
        return []
    pd_ = nodes[parent]
    inv = {v.upper(): c.upper() for c, v in d["join"].items()}
    if all(pc.upper() in inv for pc in pd_["key"]):
        pk = "|".join(row.get(inv[pc.upper()], "") for pc in pd_["key"])
        return [(parent, pk)] if (parent, pk) in index else []
    hits = sorted({key_of(pr, pd_["key"]) for pr in data[parent]
                   if all(pr.get(v.upper(), "") == row.get(c.upper(), "") for c, v in d["join"].items())})
    return [(parent, h) for h in hits]


def cmd_build(args) -> int:
    cfg = load_config(Path(args.config))
    ext, out = Path(args.extracts), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warn_if_not_ignored(out)
    mask = masker(cfg)
    nodes = cfg["nodes"]
    order = topo_order(cfg)
    data: dict[str, list[dict]] = {}
    checks: list[dict] = []
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
        # 같은 키가 여러 번 나오면 한 번만 쓴다(예: CONNECT BY가 같은 코일을 여러 번 낸 추출본).
        seen, uniq = set(), []
        for row in mrows:
            k = key_of(row, nodes[name]["key"])
            if k in seen:
                checks.append({"check": "중복 키", "node": name, "key": k, "detail": "같은 키의 행을 하나만 썼다"})
                continue
            seen.add(k)
            uniq.append(row)
        data[name] = uniq

    parents = resolve_parents(cfg, order, data, checks)
    label = {n: nodes[n].get("label", n) for n in order}

    links = []
    for (name, k), plist in parents.items():
        if not plist:
            links.append({"node": name, "label": label[name], "key": k, "parent_node": "", "parent_key": ""})
        for pn, pk in plist:
            links.append({"node": name, "label": label[name], "key": k, "parent_node": pn, "parent_key": pk})
    write_csv(out / "links.csv", ["node", "label", "key", "parent_node", "parent_key"], links)

    # 경로표: 자식이 하나도 없는 행마다 대표 부모를 따라 시작점까지 올라간다.
    has_child = {p for plist in parents.values() for p in plist}
    paths = []
    for (name, k), plist in parents.items():
        if (name, k) in has_child:
            continue
        chain, cur, visited = [], (name, k), set()
        while cur and cur not in visited:
            visited.add(cur)
            chain.append(f"{label[cur[0]]}:{cur[1]}")
            nxt = parents.get(cur) or []
            cur = nxt[0] if nxt else None
        paths.append({"leaf_node": name, "leaf_key": k, "path": " > ".join(reversed(chain))})
    write_csv(out / "paths.csv", ["leaf_node", "leaf_key", "path"], paths)

    timeline = []
    for name in order:
        d = nodes[name]
        tc = d.get("time_column")
        if not tc:
            continue
        evcols = [c.upper() for c in d.get("event_columns", []) or []]
        for row in data[name]:
            k = key_of(row, d["key"])
            t = row.get(tc.upper(), "")
            if not t:
                checks.append({"check": "시각 없음", "node": name, "key": k, "detail": tc})
            plist = parents.get((name, k)) or []
            timeline.append({"time": t, "node": name, "label": label[name], "key": k,
                             "parent": "; ".join(f"{pn}:{pk}" for pn, pk in plist),
                             "event": "; ".join(f"{c}={row.get(c, '')}" for c in evcols)})
    timeline.sort(key=lambda r: (r["time"] == "", r["time"], r["node"], r["key"]))
    write_csv(out / "timeline.csv", ["time", "node", "label", "key", "parent", "event"], timeline)

    # 계보도(Mermaid): diagram: true 인 노드만, 연결은 모두 그린다.
    shown = {n for n in order if nodes[n].get("diagram")}
    lines = ["flowchart LR"]
    for (name, k) in parents:
        if name in shown:
            lines.append(f'  {mermaid_id(name, k)}["{_mermaid_text(label[name])}<br/>{_mermaid_text(k)}"]')
    for (name, k), plist in parents.items():
        for pn, pk in plist:
            if name in shown and pn in shown:
                lines.append(f"  {mermaid_id(pn, pk)} --> {mermaid_id(name, k)}")
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
    s.add_argument("--out", default=str(TOOL_DIR / "sql"), help="기본: 도구 폴더의 sql/")
    b = sub.add_parser("build", help="내보낸 CSV로 산출물을 만든다")
    b.add_argument("--config", required=True)
    b.add_argument("--extracts", default=str(TOOL_DIR / "extracts"), help="기본: 도구 폴더의 extracts/")
    b.add_argument("--out", default=str(TOOL_DIR / "out"), help="기본: 도구 폴더의 out/")
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
