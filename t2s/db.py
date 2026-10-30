"""DB 접속·스키마 조회·실행. DB별 차이는 SQLAlchemy가 흡수한다."""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError

# SQLAlchemy 방언 이름과 sqlglot 방언 이름이 다른 경우만 적는다.
_SQLGLOT_NAME = {"postgresql": "postgres", "mssql": "tsql", "mariadb": "mysql"}
# 접속 자체를 읽기 전용으로 거는 방언. ponytail: MySQL 은 SET SESSION TRANSACTION READ ONLY, 다른 DB 는 계정 권한에 의존. 대상 DB 가 생기면 추가
_READONLY_BACKENDS = {"sqlite", "postgresql"}
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
