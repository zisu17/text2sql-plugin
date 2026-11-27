"""DB 접속·스키마 조회·실행. DB별 차이는 SQLAlchemy가 흡수한다."""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property

import sqlglot
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlglot import exp
from sqlglot.errors import SqlglotError

# SQLAlchemy 방언 이름과 sqlglot 방언 이름이 다른 경우만 적는다.
_SQLGLOT_NAME = {"postgresql": "postgres", "mssql": "tsql", "mariadb": "mysql"}
# 접속 자체를 읽기 전용으로 거는 방언. ponytail: MySQL 은 SET SESSION TRANSACTION READ ONLY, 다른 DB 는 계정 권한에 의존. 대상 DB 가 생기면 추가
_READONLY_BACKENDS = {"sqlite", "postgresql"}
# 다른 표의 행마다 큰 표를 처음부터 다시 읽는 횟수가 이만큼이면 실행 전에 알린다. 올리스트 SQLite 실측: 3억 번에 17초, 800만 번에 0.4초
REPEATED_READS = 1e8
# 원본 컬럼 타입 문자열 → Ossie 자료형. 위에서부터 먼저 맞는 것
_FAMILIES = [
    (r"TIMESTAMP.*TIME ZONE|TIMESTAMPTZ|TIMESTAMP_TZ", "DateTimeTz"),
    (r"TIMESTAMP|DATETIME", "DateTime"),
    (r"^DATE\b", "Date"),
    (r"^TIME\b", "Time"),
    (r"BOOL", "Boolean"),
    (r"INT|SERIAL", "Integer"),
    (r"NUMERIC|DECIMAL|NUMBER|MONEY", "Decimal"),
    (r"REAL|FLOAT|DOUBLE", "Float"),
    (r"CHAR|TEXT|STRING|CLOB|UUID|JSON|ENUM", "String"),
]


def type_family(type_name: str) -> str:
    """DB 타입 문자열 → Ossie 자료형 이름. 모르면 unknown."""
    up = type_name.upper()
    return next((fam for pat, fam in _FAMILIES if re.search(pat, up)), "unknown")


@dataclass
class Database:
    url: str
    schema: str | None = None
    dialect_override: str | None = None  # 드라이버 이름과 실제 문법이 다를 때(예: hive 드라이버로 spark 접속)
    max_rows: int = 100
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls, **overrides) -> Database:
        """DATABASE_URL, DB_SCHEMA, SQL_DIALECT, MAX_ROWS, QUERY_TIMEOUT 환경변수로 만든다. overrides 가 환경변수보다 앞선다."""
        env = dict(url=os.environ["DATABASE_URL"], schema=os.getenv("DB_SCHEMA") or None, dialect_override=os.getenv("SQL_DIALECT") or None,
                   max_rows=int(os.getenv("MAX_ROWS", "100")), timeout_s=float(os.getenv("QUERY_TIMEOUT", "30")))
        return cls(**{**env, **overrides})

    @cached_property
    def engine(self) -> Engine:
        """접속을 읽기 전용으로 건다. 가드를 우회한 쓰기도 DB 가 거절한다."""
        url = make_url(self.url)
        backend = url.get_backend_name()
        if backend == "postgresql":  # psycopg2·psycopg(3) 모두 options 를 받는다
            return create_engine(self.url, connect_args={"options": "-c default_transaction_read_only=on"})
        if backend != "sqlite":
            return create_engine(self.url)
        # SQLite 는 없는 파일에 접속하면 빈 DB 파일을 새로 만든다. 경로 오타가 "테이블 없음" 으로 보이지 않게 먼저 막는다
        if url.database and url.database != ":memory:" and not url.database.startswith("file:") and not os.path.exists(url.database):
            raise ArgumentError(f"SQLite 파일이 없다: {url.database}")
        engine = create_engine(self.url)

        def _sqlite_readonly(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA query_only = 1")
            # 정렬·GROUP BY 임시 데이터를 파일 대신 메모리에 둔다. 쓰기 금지 샌드박스(Codex -s read-only)에서 임시 파일을 못 만들면 disk I/O error 가 난다
            dbapi_conn.execute("PRAGMA temp_store = MEMORY")

        event.listen(engine, "connect", _sqlite_readonly)
        return engine

    @property
    def readonly_guarantee(self) -> str:
        """읽기 전용을 누가 보장하는지. connection: 접속 옵션으로 강제, account: DB 계정 권한에 맡김."""
        return "connection" if make_url(self.url).get_backend_name() in _READONLY_BACKENDS else "account"

    @cached_property
    def dialect(self) -> str:
        """sqlglot 문법 이름."""
        if self.dialect_override:
            return self.dialect_override
        name = self.engine.dialect.name
        return _SQLGLOT_NAME.get(name, name)

    @cached_property
    def _insp(self):
        return inspect(self.engine)

    @cached_property
    def tables(self) -> dict[str, str]:
        """소문자 테이블명 → 실제 테이블명. 검증의 허용 목록으로도 쓴다."""
        names = self._insp.get_table_names(schema=self.schema) + self._insp.get_view_names(schema=self.schema)
        return {n.lower(): n for n in names}

    def _qualified(self, name: str) -> str:
        return f"{self.schema}.{name}" if self.schema else name

    def table_list(self, only: set[str] | None = None) -> str:
        """only(소문자 이름)를 주면 그 테이블만 보여 준다."""
        lines = []
        for key, name in self.tables.items():
            if only is not None and key not in only:
                continue
            comment = self._try(lambda: self._insp.get_table_comment(name, schema=self.schema).get("text"), None)
            lines.append(self._qualified(name) + (f": {comment}" if comment else ""))
        return "\n".join(lines) or "조회 가능한 테이블 없음"

    def describe(self, names: list[str], only: set[str] | None = None) -> str:
        blocks = []
        for raw in names:
            key = raw.split(".")[-1].lower()
            name = self.tables.get(key) if only is None or key in only else None
            if name is None:
                blocks.append(f"## {raw}\n없는 테이블")
                continue
            lines = [f"## {self._qualified(name)}"]
            for c in self._insp.get_columns(name, schema=self.schema):
                lines.append(f"- {c['name']} {self.type_name(c['type'])}" + (f"  -- {c['comment']}" if c.get("comment") else ""))
            pk = self._try(lambda: self._insp.get_pk_constraint(name, schema=self.schema).get("constrained_columns"), [])
            if pk:
                lines.append(f"PK: {', '.join(pk)}")
            for fk in self._try(lambda: self._insp.get_foreign_keys(name, schema=self.schema), []):
                lines.append(f"FK: ({', '.join(fk['constrained_columns'])}) -> {fk['referred_table']}({', '.join(fk['referred_columns'])})")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def execute(self, sql: str) -> dict:
        with self.engine.connect() as conn, self._time_limit(conn):  # 커밋하지 않고 닫으므로 롤백된다
            result = conn.execute(text(sql))
            rows = result.fetchmany(self.max_rows + 1)
            columns = list(result.keys())
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[: self.max_rows]],
            "truncated": len(rows) > self.max_rows,
        }

    def slow_parts(self, sql: str, upper: bool = False) -> list[dict]:
        """실행하지 않고 실행 계획만 본다. 다른 표의 행마다 큰 표를 처음부터 다시 읽는 곳(반복 읽기 REPEATED_READS 이상)을 돌려준다.
        [{"table", "times": 다시 읽는 횟수, "rows": 한 번에 읽는 행 수}]. 계획을 읽지 못하면 빈 목록이다(실행을 막을 근거가 없다).
        upper 면 행 수를 모르는 CTE·하위 쿼리를 그 안에서 읽는 가장 큰 표의 행 수로 잡는다(위로 어림). 실행 전 점검에는 쓰지 않는다
        (묶음 결과를 크게 잡아 빠른 SQL 도 막는다). 이미 시간을 넘긴 SQL 에서 어디를 반복해 읽었는지 알려 줄 때 쓴다.
        ponytail: SQLite 만 본다. PostgreSQL 은 해시 조인을 쓰므로 상관 하위 쿼리만 남는다. 대상이 생기면 EXPLAIN (FORMAT JSON) 으로 붙인다"""
        if self.dialect != "sqlite":
            return []
        try:
            with self.engine.connect() as conn, self._time_limit(conn):
                plan = conn.execute(text("EXPLAIN QUERY PLAN " + sql)).fetchall()

                def count(t: exp.Table) -> float | None:
                    try:
                        return conn.execute(text(exp.select("MAX(rowid)").from_(t.copy()).sql("sqlite"))).scalar()
                    except SQLAlchemyError:  # 뷰·WITHOUT ROWID 표는 rowid 가 없다
                        return None

                tree = sqlglot.parse_one(sql, read="sqlite")
                size = _sizes(tree, count, upper)
        except (SQLAlchemyError, SqlglotError):
            return []
        real = {t.alias_or_name.lower(): t.name for t in tree.find_all(exp.Table)}  # 계획에는 별칭이 나온다
        found = _repeats(plan, size, _streams(tree))
        return [{**f, "table": real.get(f["table"].lower(), f["table"])} for f in found]

    @contextmanager
    def _time_limit(self, conn):
        """실행 시간 제한. 넘으면 DB 오류(취소)로 끝난다."""
        if self.dialect == "postgres":
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(self.timeout_s * 1000)}")
            yield
        elif self.dialect == "sqlite":
            raw = conn.connection.driver_connection
            deadline = time.monotonic() + self.timeout_s
            raw.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
            try:
                yield
            finally:
                raw.set_progress_handler(None, 0)
        else:
            yield  # ponytail: 제한 없음. MySQL max_execution_time 등은 대상 DB 가 생기면 추가

    def type_name(self, t) -> str:
        """SQLAlchemy 타입 객체 → 이 DB 문법의 타입 문자열."""
        try:
            return str(t.compile(dialect=self.engine.dialect))
        except Exception:  # 방언이 모르는 타입(NullType 등)
            return type(t).__name__

    @staticmethod
    def _try(fn, default):
        """일부 DB는 주석·키 조회를 지원하지 않는다."""
        try:
            return fn() or default
        except NotImplementedError:
            return default


def _streams(tree: exp.Expression) -> bool:
    """맨 바깥이 LIMIT 만 있고 정렬·묶음·집계가 없으면 앞 행을 찾는 대로 멈춘다. 그 반복은 몇 번 돌지 모른다(조회 질문은 거의 다 이렇다)."""
    return (isinstance(tree, exp.Select) and bool(tree.args.get("limit")) and not any(tree.args.get(k) for k in ("order", "group", "distinct", "having"))
            and not any(a.find_ancestor(exp.Select) is tree for a in tree.find_all(exp.AggFunc, exp.Window)))


def _sizes(tree: exp.Expression, count, upper: bool = False):
    """실행 계획에 나오는 이름(표·별칭·CTE·하위 쿼리 별칭) → 행 수. 모르면 None 이다. 표는 count(표) 로 센다.
    CTE·하위 쿼리는 묶음 없는 집계면 1행, LIMIT 이 있으면 그 수 이하, 조건·조인·묶음 없이 한 표를 그대로 옮기면 그 표의 행 수다.
    그 밖(조건·묶음·DISTINCT 가 있는 것)은 모른다. 묶음 결과는 몇 행일지 몰라 크게 잡으면 빠른 SQL 도 막는다(평가셋 bcds-001·hard-003).
    upper 면 모르는 것을 그 안에서 읽는 가장 큰 표(CTE 포함)의 행 수로 잡는다. 조건·묶음은 행을 늘리지 않으므로 위 어림이다."""
    ctes = {c.alias: c.this for c in tree.find_all(exp.CTE)}
    base = {}
    for t in tree.find_all(exp.Table):
        if t.name not in ctes and t.name not in base:
            base[t.name] = count(t)
    memo: dict[str, float | None] = {}

    def rows(q: exp.Expression) -> float | None:
        if not isinstance(q, exp.Select):
            return None
        if not q.args.get("group") and any(a.find_ancestor(exp.Select) is q and not a.find_ancestor(exp.Window) for a in q.find_all(exp.AggFunc)):
            return 1
        n, src = None, q.args.get("from_")
        if src and not any(q.args.get(k) for k in ("where", "group", "having", "distinct", "joins", "laterals")):
            n = table(src.this) if isinstance(src.this, exp.Table) else rows(src.this.this) if isinstance(src.this, exp.Subquery) else None
        limit = q.args.get("limit")
        cap = limit.expression if isinstance(limit, exp.Limit) else None
        if isinstance(cap, exp.Literal) and cap.is_int:
            return min(int(cap.name), n) if n is not None else int(cap.name)
        if n is None and upper:
            known = [m for t in q.find_all(exp.Table) if (m := table(t)) is not None]
            n = max(known) if known else None
        return n

    def table(t: exp.Table) -> float | None:
        if t.name in ctes:
            if t.name not in memo:
                memo[t.name] = None  # 재귀 CTE 는 모른다
                memo[t.name] = rows(ctes[t.name])
            return memo[t.name]
        return base.get(t.name)

    seen: dict[str, set] = {}
    for t in tree.find_all(exp.Table):
        seen.setdefault(t.alias_or_name.lower(), set()).add(table(t))
    for sub in tree.find_all(exp.Subquery):
        if sub.alias:
            seen.setdefault(sub.alias.lower(), set()).add(rows(sub.this))
    names = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}  # 같은 별칭이 다른 크기의 표를 가리키면 모른다
    return lambda name: names.get(name.lower())


def _plain(detail: str) -> str:
    """SQLite 3.36 이전 표기(SCAN TABLE orders AS o, SEARCH TABLE x ..., SCAN SUBQUERY 2 AS x, EXECUTE ...)를 지금 표기(SCAN o)로 맞춘다."""
    d = re.sub(r"^EXECUTE ", "", detail)
    d = re.sub(r"^(SCAN|SEARCH) TABLE \S+ AS (\S+)", r"\1 \2", d)
    d = re.sub(r"^(SCAN|SEARCH) (?:TABLE |SUBQUERY \d+ AS )", r"\1 ", d)
    return d


def _repeats(plan: list, size, lazy: bool = False) -> list[dict]:
    """SQLite EXPLAIN QUERY PLAN 행 (id, parent, _, detail) 에서 반복 읽기를 찾는다. 같은 부모 아래 SCAN·SEARCH 는 앞에서부터 바깥 → 안쪽
    반복이다. 안쪽 SCAN 은 바깥 반복의 행마다 표 전체를 읽는다(SEARCH 는 색인으로 몇 행만 읽는다고 본다). 상관 하위 쿼리는 바깥 행마다 돈다.
    상관이 없는 하위 쿼리는 한 번만 돈다. 바깥 표의 WHERE 로 줄어드는 행은 모르므로 표 전체 행으로 본다(--force 로 그대로 돌린다).
    lazy 면 맨 바깥 반복은 LIMIT 을 채우면 멈추므로 알리지 않는다. MATERIALIZE 와 상관없는 하위 쿼리는 그래도 끝까지 돈다."""
    kids: dict[int, list] = {}
    for i, parent, _, detail in plan:
        kids.setdefault(parent, []).append((i, detail))
    found = []

    def walk(node: int, outer: float, lazy: bool) -> None:
        times = outer
        for i, d in kids.get(node, []):
            d = _plain(d)
            loop = re.match(r"(SCAN|SEARCH) (\S+)", d)
            if loop and not d.startswith("SCAN CONSTANT ROW"):
                n = size(loop[2]) if loop[1] == "SCAN" else 1
                if loop[1] == "SCAN" and n is not None and not lazy and times > 1 and times * n >= REPEATED_READS:
                    found.append({"table": loop[2], "times": int(times), "rows": int(n)})
                walk(i, times, lazy)
                times *= n or 1  # 크기를 모르는 반복은 곱하지 않는다. 확실할 때만 알린다
            elif "CORRELATED" in d:
                walk(i, times, lazy)
            elif re.search(r"(SCALAR|LIST) SUBQUERY", d):
                walk(i, 1, False)
            elif d.startswith("MATERIALIZE"):  # 끝까지 만들어 둔다
                walk(i, outer, False)
            else:  # CO-ROUTINE·COMPOUND 등은 감싼 곳만큼, 감싼 곳처럼 돈다
                walk(i, outer, lazy)

    walk(0, 1, lazy)
    return found
