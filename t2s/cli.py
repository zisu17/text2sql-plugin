"""플랫폼 공통 명령줄 진입점 `t2s`. Claude Code·Codex 스킬이 셸에서 불러 JSON 을 받는다. LLM 을 쓰지 않는다.

DB 는 DATABASE_URL·DB_SCHEMA·SQL_DIALECT·MAX_ROWS·QUERY_TIMEOUT, 정의는 SEMANTIC_MODEL 환경변수로 받는다.
모든 출력은 stdout 에 JSON 하나다. T2S_LOG 에 파일 경로가 있으면 호출마다 사용 기록 JSON 한 줄을 덧붙인다.

t2s context
t2s validate "SELECT order_id FROM orders" --read duckdb
t2s compile revenue --by products.product_category_name --run
t2s values customers.customer_city --like paulo
t2s check --static
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import sqlglot
from sqlalchemy.exc import ArgumentError, NoSuchModuleError, SQLAlchemyError
from sqlglot import exp
from sqlglot.errors import SqlglotError

from . import engine
from .db import Database, type_family
from .definition import findings, uncovered
from .engine import BUILD, EngineError, SemanticModel
from .guard import UnsafeSQL, lint, policy, prepare, shape, table_names
from .ossie import READ, expr_sql, ext, is_time, read_yaml

# 가드 거절 중 SQL 을 고쳐 다시 시도해도 되는 것. policy 는 질문의 뜻을 바꿔야 해서 다시 시도하지 않는다
_RETRYABLE = {"syntax", "unknown_table", "unknown_column", "unknown_join", "missing_object", "type_mismatch"}
# ponytail: 메시지 정규식 분류. 문법 오류와 방언 오류는 구분하지 못한다
_DB_ERRORS = [
    ("missing_object", re.compile(r"no such table|no such column|does not exist", re.I)),
    ("syntax", re.compile(r"syntax error", re.I)),
    ("timeout", re.compile(r"interrupted|statement timeout|canceling statement", re.I)),
    ("type_mismatch", re.compile(r"datatype mismatch|operator does not exist|invalid input syntax|cannot be cast", re.I)),
]
# 없는 DB 드라이버 모듈 → 설치할 패키지
_DRIVERS = {"psycopg": "psycopg[binary]", "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "pymysql": "pymysql"}
_DATETIME = {"DateTime", "DateTimeTz"}  # 날짜만 있는 Date 는 = '날짜' 비교가 맞다
_log: dict = {}  # 이번 호출의 사용 기록(T2S_LOG). main 이 끝날 때 한 줄로 쓴다


def _emit(obj: dict) -> None:
    _log["out"] = obj
    print(json.dumps(obj, ensure_ascii=False, default=str))


def _fail(obj: dict) -> None:
    _emit(obj)
    sys.exit(1)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # 인자 오류도 stdout 에 JSON 하나로 낸다
        _fail({"error": {"kind": "bad_args", "message": f"{message}. 사용법: {self.format_usage().strip().removeprefix('usage: ')}", "retryable": True}})


def _database() -> Database:
    if not os.getenv("DATABASE_URL"):
        _fail({"error": {"kind": "no_database", "message": "DATABASE_URL 환경변수가 없다. 읽기 전용 계정의 접속 주소를 셸에서 설정한다. 예: export DATABASE_URL=sqlite:////절대/경로/파일.db 또는 export DATABASE_URL=postgresql+psycopg://계정:비밀번호@호스트:5432/DB이름"}})
    try:
        return Database.from_env()
    except ValueError:
        _fail({"error": {"kind": "no_database", "message": "MAX_ROWS 는 정수, QUERY_TIMEOUT 은 초 단위 숫자여야 한다"}})


def _connect_error(e: Exception) -> dict:
    """실행 전 접속 단계(주소·드라이버·네트워크) 오류. 실행 중 DB 오류는 _db_error 가 분류한다."""
    if isinstance(e, ImportError):
        module = e.name or str(e)
        message = (f"DB 드라이버 {module} 가 없다. README 의 PostgreSQL 절에 적힌 대로 드라이버를 t2s 환경에 설치한 뒤 다시 실행한다: "
                   f"uv pip install --python {sys.executable} '{_DRIVERS.get(module, module)}'")
    elif isinstance(e, NoSuchModuleError):
        message = f"DATABASE_URL 의 DB 종류를 모른다({e}). 예: sqlite:///파일.db, postgresql://계정@호스트/DB이름"
    elif isinstance(e, ArgumentError):
        message = f"DATABASE_URL 을 확인한다: {e}"
    else:
        message = f"DB 에 접속하지 못했다. 주소·계정·네트워크를 확인한다: {str(e.__cause__ or e)[:500]}"
    return {"kind": "db", "message": message, "retryable": False}


def _load(path: str) -> SemanticModel:
    """정의 파일을 읽는다. 못 읽어도 JSON 오류 하나로 낸다(출력 계약)."""
    try:
        return SemanticModel.load(path)
    except Exception as e:  # 파일 없음, YAML 오류, 필수 키 누락 모두 설정 오류다
        _fail({"error": {"kind": "bad_definition", "message": f"정의 파일을 읽을 수 없음: {path} ({type(e).__name__}: {str(e)[:200]})"}})


def _model() -> SemanticModel | None:
    path = os.getenv("SEMANTIC_MODEL")
    return _load(path) if path else None


def _sql_arg(sql: str) -> str:
    return sys.stdin.read() if sql == "-" else sql


def _check(db: Database, sql: str, read: str | None, engine_sql: bool = False) -> tuple[str | None, UnsafeSQL | None]:
    """가드 검사. (통과한 SQL, None) 또는 (None, 거절)."""
    try:
        rules = policy(db, _model(), engine_sql)
        return prepare(sql, db.dialect, max_rows=db.max_rows, schema=db.schema, read=read, **rules), None
    except UnsafeSQL as e:
        return None, e


def _lint(db: Database, model: SemanticModel | None, sql: str, read: str | None) -> list[dict]:
    """경고. 시간 컬럼·연결은 정의가 있으면 정의(is_time 필드 중 식이 컬럼 하나인 것, 승인된 연결), 없으면 DB 컬럼 타입과 FK."""
    try:
        return _lint_inputs(db, model, sql, read)
    except (SqlglotError, SQLAlchemyError, ValueError, KeyError):  # 경고는 덤이다. 실패해도 명령은 그대로 간다
        return []


def _lint_inputs(db: Database, model: SemanticModel | None, sql: str, read: str | None) -> list[dict]:
    if model:
        times = set()
        for (d, _), f in model.fields.items():
            if is_time(f) and f.get("datatype") != "Date":
                e = sqlglot.parse_one(expr_sql(f), read=READ)
                if isinstance(e, exp.Column):
                    times.add(f"{model.datasets[d]['source'].rpartition('.')[2].lower()}.{e.name.lower()}")
        return lint(sql, db.dialect, read, times, policy(db, model)["edges"])
    times, edges = set(), {}
    for t in shape(sql, db.dialect, read)["tables"]:  # 쓴 테이블만 DB 에 묻는다
        if t not in db.tables:
            continue
        name = db.tables[t]
        times |= {f"{t}.{c['name'].lower()}" for c in db._insp.get_columns(name, schema=db.schema)
                  if type_family(db.type_name(c["type"])) in _DATETIME}
        edges[t] = [{"from": t, "to": fk["referred_table"].lower(), "from_columns": fk["constrained_columns"],
                     "to_columns": fk["referred_columns"]} for fk in db._insp.get_foreign_keys(name, schema=db.schema)]
    return lint(sql, db.dialect, read, times, edges)


def _guard_error(e: UnsafeSQL) -> dict:
    return {"kind": e.kind, "message": str(e), "retryable": e.kind in _RETRYABLE}


def _db_error(e: SQLAlchemyError) -> dict:
    message = str(e.__cause__ or e)[:1000]
    kind = next((k for k, pattern in _DB_ERRORS if pattern.search(message)), "db")
    if kind == "timeout":
        message += " — 조건이나 LIMIT 을 더해 범위를 줄여라"
    return {"kind": kind, "message": message, "retryable": kind in _RETRYABLE}


def _execute(db: Database, safe: str) -> dict:
    """가드를 통과한 SQL 을 실행한다. run 과 compile --run 이 같이 쓴다."""
    start = time.monotonic()
    try:
        result = db.execute(safe)
    except SQLAlchemyError as e:
        return {"ok": False, "error": _db_error(e)}
    return {"ok": True, "executed_sql": safe, **result, "elapsed_ms": round((time.monotonic() - start) * 1000, 1)}


def _compile(a: argparse.Namespace) -> None:
    """지표 SQL 은 엔진이 만든다. --run 이면 엔진 SQL 을 run 과 같은 실행·오류 분류로 돌린다."""
    path = os.getenv("SEMANTIC_MODEL")
    if not path:
        _fail({"error": {"kind": "no_definition", "message": "SEMANTIC_MODEL 환경변수에 정의 파일 경로가 없다"}})
    call = {"metrics": a.metrics, "dimensions": a.by, "where": a.where, "order_by": a.order}
    if a.limit is not None:
        call["limit"] = a.limit
    else:
        a.limit = 100
    _log.update(call=call, expr=a.expr, executed=a.run, preview=a.allow_draft)
    db = None
    if a.run:
        db = _database()
        a.dialect = db.dialect
    model = _load(path)
    times = {f"{d}.{n}".lower() for (d, n), f in model.fields.items() if is_time(f) and f.get("datatype") != "Date"}
    warnings = [w for cond in a.where for w in lint(cond, READ, time_columns=times)]
    if a.allow_draft:  # 승인 전에 결과를 보고 고치려는 미리 보기다. 질문 답변(ask)에서는 쓰지 않는다
        warnings.append({"kind": "preview", "message": "승인 전 지표·연결로 계산한 미리 보기다. 질문에 답할 때는 쓰지 않는다"})
    try:
        out = {**engine.build(model, a), "warnings": warnings, **({"preview": True} if a.allow_draft else {})}
    except EngineError as e:
        _fail({"error": e.to_dict(), "warnings": warnings})
    _log["sql"] = out.get("sql")
    if db is None or "sql" not in out:
        return _emit(out)
    safe, err = _check(db, out["sql"], None, engine_sql=True)  # 엔진이 만든 SQL: 테이블·조회 전용·행 수 제한만 본다
    if err:
        _fail({**out, "ok": False, "error": _guard_error(err)})
    result = _execute(db, safe)
    if not result["ok"]:
        _fail({**out, **result})
    _emit({**out, "result": result})


def _values(db: Database, model: SemanticModel | None, ref: str, like: str | None, limit: int) -> None:
    """필드(정의가 없으면 컬럼)의 실제 값. SQL 은 sqlglot 노드로 만들어 --like 문자열이 SQL 로 읽히지 않게 하고 가드를 거친다."""
    def fail(kind: str, message: str) -> None:
        _fail({"error": {"kind": kind, "message": message, "retryable": kind in _RETRYABLE}})

    d, _, n = ref.partition(".")
    if model:
        if d not in model.datasets:
            fail("unknown_table", f"없는 데이터셋: {d} (데이터셋: {', '.join(model.datasets)})")
        try:
            e, source = model._field(d, n), model._table(d)
        except EngineError as x:
            fail("unknown_column" if x.code == "unknown_field" else "bad_definition", f"{x} {x.candidates or ''}".strip())
        f = model.fields[(d, n)]
        if f.get("sensitivity") or ext(f).get("sensitivity"):
            fail("policy", f"{ref} 는 민감 정보로 표시돼 값을 보여 주지 않는다")
        table, timed = model.datasets[d]["source"], is_time(f)
    else:
        table = db.tables.get(d.lower())
        if not table:
            fail("unknown_table", f"없는 테이블: {d}")
        col = next((c for c in db._insp.get_columns(table, schema=db.schema) if c["name"].lower() == n.lower()), None)
        if col is None:
            fail("unknown_column", f"{table} 에 없는 컬럼: {n}")
        e, source = exp.column(col["name"], table=table), exp.table_(table, db=db.schema)
        timed = type_family(db.type_name(col["type"])) in {"Date", "Time", *_DATETIME}
    column = e.name if isinstance(e, exp.Column) else e.sql(BUILD)
    if timed:
        q = exp.select(exp.Min(this=e.copy()).as_("lo"), exp.Max(this=e.copy()).as_("hi"),
                       exp.Count(this=e.copy()).as_("nn"), exp.Count(this=exp.Star()).as_("n"))
    else:
        q = exp.select(e.copy().as_("v"), exp.Count(this=exp.Star()).as_("n")).group_by(e.copy())
        q = q.order_by(exp.Ordered(this=exp.column("n"), desc=True), exp.column("v")).limit(limit + 1)
        if like is not None:
            q = q.where(exp.Like(this=exp.Lower(this=e.copy()), expression=exp.Lower(this=exp.Literal.string(f"%{like}%"))))
    q = q.from_(source, dialect=BUILD)
    safe, err = _check(db, q.sql(BUILD), BUILD, engine_sql=True)  # 엔진 SQL 과 같은 정책: 허용 테이블, 행 수 제한
    if err:
        _fail({"error": _guard_error(err)})
    try:
        result = db.execute(safe)
    except SQLAlchemyError as x:
        _fail({"error": _db_error(x)})
    head = {"ref": ref, "table": table, "column": column}
    if timed:
        lo, hi, nn, rows = result["rows"][0]
        return _emit({**head, "kind": "time", "min": lo, "max": hi, "non_null": nn, "rows": rows})
    rows = result["rows"]
    values = [{"value": v[:80] if isinstance(v, str) else v, "count": c} for v, c in rows[:limit]]
    _emit({**head, "kind": "categorical", "values": values, "truncated": len(rows) > limit or result["truncated"]})


def _check_definition(path: str | None, static: bool) -> None:
    """정의를 검사한다. 문제가 있어도 exit 0(validate 와 같다). 파일을 못 읽을 때만 bad_definition."""
    if not path:
        _fail({"error": {"kind": "no_definition", "message": "정의 파일 경로도 SEMANTIC_MODEL 환경변수도 없다"}})
    try:
        doc = read_yaml(path)
        if not isinstance(doc, dict):
            raise ValueError("YAML 최상위가 매핑이 아님")
    except Exception as e:  # 파일 없음, YAML 오류
        _fail({"error": {"kind": "bad_definition", "message": f"정의 파일을 읽을 수 없음: {path} ({type(e).__name__}: {str(e)[:200]})"}})
    db = None if static else _database()
    if db:
        db.tables  # 접속 실패를 항목마다 문제로 늘어놓지 않고 오류 하나로 낸다
    r = findings(doc, db)
    try:
        model = SemanticModel(doc)
    except Exception:  # 형식 오류는 findings 가 문제로 보고했다
        model = None
    gaps = {"tables": [], "columns": {}}
    if db and model:
        try:
            gaps = uncovered(model, db)
        except (SqlglotError, KeyError, ValueError):  # 읽지 못하는 필드 식은 findings 가 문제로 보고했다
            pass
    _emit({"ok": r["ok"], "mode": "static" if static else "db",
           "definition": {"name": doc.get("name"), "revision": model.revision if model else None, "path": path},
           "problems": r["problems"], "notes": r["notes"], "pending": r.get("pending") or [], "samples": r["samples"] or [], "uncovered": gaps})


def _context(db: Database, model: SemanticModel | None) -> dict:
    # ponytail: 정의 요약이 1만 토큰 이하면 통째로 넘긴다. 넘으면 질문으로 검색하는 구현으로 바꾼다
    return {
        "dialect": db.dialect, "schema": db.schema, "max_rows": db.max_rows, "readonly_guarantee": db.readonly_guarantee,
        "definition": {"name": model.name, "revision": model.revision, "path": os.getenv("SEMANTIC_MODEL")} if model else None,
        "catalog": model.catalog() if model else None,
        "tables": sorted(policy(db, model)["allowed_tables"]),
    }


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="t2s", description="Text2SQL 공통 명령. 결과는 stdout 에 JSON 하나")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("context", help="문법·스키마·정의 요약·조회 가능한 테이블").add_argument("question", nargs="?", help="질문(지금은 쓰지 않는다)")
    sub.add_parser("schema", help="테이블 목록, 테이블을 주면 컬럼 설명").add_argument("tables", nargs="*")
    for name, text in (("validate", "SQL 검증 결과와 구조(실패해도 exit 0)"), ("run", "검증 후 실행")):
        v = sub.add_parser(name, help=text)
        v.add_argument("sql", help="SQL. - 면 stdin")
        v.add_argument("--read", help="SQL 을 쓴 문법. 없으면 대상 DB 문법")
    v = sub.add_parser("compile", help="지표 SQL 만들기(정의 파일은 SEMANTIC_MODEL)")
    v.add_argument("metrics", nargs="*", help="지표 이름. 비우면 지표 목록을 보여준다")
    v.add_argument("--by", action="append", default=[], help="묶음 기준 dataset.field. 날짜는 dataset.field__month")
    v.add_argument("--where", action="append", default=[], help="조건. dataset.field 로 쓴다")
    v.add_argument("--order", action="append", default=[], help="정렬할 결과 컬럼. 내림차순은 --order=-컬럼")
    v.add_argument("--limit", type=int, help="행 수(기본 100)")
    v.add_argument("--dialect", default="sqlite", help="SQL 문법. --run 이면 DB에서 정한다")
    v.add_argument("--run", action="store_true", help="DATABASE_URL 에 실행한다")
    v.add_argument("--allow-draft", action="store_true", help="미리 보기: 승인 전 지표와 연결도 쓴다. 답변에는 쓰지 않는다")
    v.add_argument("--expr", help="지표 결과 컬럼으로 하는 식. 예: 'revenue / order_count'. value 컬럼으로 나온다")
    v = sub.add_parser("values", help="필드의 실제 값(범주형은 값별 행 수, 시간형은 범위)")
    v.add_argument("ref", help="정의가 있으면 dataset.field, 없으면 table.column")
    v.add_argument("--like", help="대소문자 무시 부분 일치")
    v.add_argument("--limit", type=int, default=20, help="값 개수(최대 100)")
    c = sub.add_parser("check", help="정의 검사(문제가 있어도 exit 0)")
    c.add_argument("path", nargs="?", help="정의 파일. 없으면 SEMANTIC_MODEL")
    c.add_argument("--static", action="store_true", help="DB 없이 정의만 본다")
    return p


def _write_log(code) -> None:
    """사용 기록 한 줄. 쓰기 실패는 무시한다(출력 계약을 깨지 않는다)."""
    path = os.getenv("T2S_LOG")
    if not path:
        return
    out = _log.pop("out", None) or {}
    err = out.get("error") or (out.get("problems") or [{}])[0]
    line = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "cmd": _log.get("cmd"),
            "ok": code in (0, None) and out.get("ok") is not False and "error" not in out, "executed": _log.get("executed", False),
            "sql": _log.get("sql"), "call": _log.get("call"), "expr": _log.get("expr"), "preview": _log.get("preview", False), "error_kind": err.get("kind") or err.get("code")}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    _log.clear()
    _log["cmd"] = argv[0] if argv else None
    code = 0
    try:
        _main(argv)
    except (SQLAlchemyError, ImportError) as e:  # 실행 전 접속 단계에서 난 오류. 실행 중 오류는 각 명령이 분류해 냈다
        code = 1
        _fail({"error": _connect_error(e)})
    except BaseException as e:
        code = e.code if isinstance(e, SystemExit) else 1
        raise
    finally:
        _write_log(code)


def _main(argv: list[str]) -> None:
    a = _parser().parse_args(argv)
    if a.cmd == "compile":
        return _compile(a)
    if a.cmd == "check":
        return _check_definition(a.path or os.getenv("SEMANTIC_MODEL"), a.static)
    db, model = _database(), _model()
    if a.cmd == "context":
        _emit(_context(db, model))
    elif a.cmd == "schema":
        only = table_names(policy(db, model)["allowed_tables"])
        _emit({"describe": db.describe(a.tables, only=only)} if a.tables else {"tables": db.table_list(only=only)})
    elif a.cmd == "validate":
        sql = _sql_arg(a.sql)
        safe, err = _check(db, sql, a.read)
        problems = [{"kind": err.kind, "message": str(err)}] if err else []
        _emit({"ok": err is None, "sql": safe, "problems": problems, **shape(sql, db.dialect, a.read),
               "warnings": _lint(db, model, sql, a.read)})
    elif a.cmd == "run":
        sql = _log["sql"] = _sql_arg(a.sql)
        _log["executed"] = True  # 가드 거절도 실행 시도로 센다
        warnings = _lint(db, model, sql, a.read)
        safe, err = _check(db, sql, a.read)
        if err:
            _fail({"ok": False, "error": _guard_error(err), "warnings": warnings})
        result = _execute(db, safe)
        (_emit if result["ok"] else _fail)({**result, "warnings": warnings})
    elif a.cmd == "values":
        _values(db, model, a.ref, a.like, max(1, min(a.limit, 100)))


if __name__ == "__main__":
    main()
