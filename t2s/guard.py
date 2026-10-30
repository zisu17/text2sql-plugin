"""실행 전 SQL 검증. 문법별 차이는 sqlglot이 흡수한다.

1차 방어일 뿐이다. 실제 보장은 DB의 읽기 전용 계정으로 한다.
"""
from __future__ import annotations

import datetime as dt
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify

_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create,
    exp.Alter, exp.TruncateTable, exp.Grant, exp.Command, exp.Into, exp.Lock,
)
# 문자열 안의 SQL 을 실행하거나(query_to_xml) 파일·외부 연결·설정·환경변수·시퀀스·잠금·지연에 닿는 함수. 이름 앞부분으로 막는다.
# ponytail: 거부 목록이라 새 함수는 놓친다. 대상 DB 가 늘면 방언별 허용 목록으로 바꾼다
_DENIED_FUNCS = (
    "pg_", "dblink", "lo_", "query_to_", "table_to_", "cursor_to_", "database_to_", "schema_to_",
    "read_", "pragma_", "load_extension", "set_config", "readfile", "writefile", "sleep", "benchmark",
    "nextval", "setval", "load_file", "get_lock", "release_lock", "getenv",
)
# sqlglot 이 AggFunc 로 분류하지 않고 Anonymous 로 읽는 집계 함수. 행 조회 정책에서 같이 막는다.
# ponytail: 이름 목록이라 방언이 늘면 빠질 수 있다. sqlglot 이 AggFunc 로 알게 되면 여기서 뺀다
_ANON_AGGS = (
    "total", "every", "jsonb_agg", "jsonb_object_agg", "json_object_agg", "xmlagg", "range_agg", "range_intersect_agg",
    "bit_xor", "bit_and", "bit_or", "bool_or", "median", "mode", "percentile_disc", "percentile_cont", "corr", "covar_", "regr_",
    "histogram", "entropy", "mad", "product", "fsum", "favg", "kahan_sum", "sumkahan", "sum_no_overflow", "geomean", "geometric_mean",
    "listagg", "json_arrayagg", "bitstring_agg", "reservoir_quantile", "std",
)


class UnsafeSQL(ValueError):
    """kind: syntax, unknown_table, unknown_column, unknown_join, policy(질문의 뜻을 바꿔야 하는 거절)."""

    def __init__(self, message: str, kind: str = "policy"):
        super().__init__(message)
        self.kind = kind


def policy(db, model=None, engine_sql: bool = False) -> dict:
    """prepare 에 넘길 허용 범위. t2s 의 validate·run·values·check 가 같이 쓴다. db 는 Database, model 은 SemanticModel 또는 None.

    정의가 있으면 정의의 테이블만 본다. 엔진이 만든 SQL(engine_sql)은 집계를 허용하고, 그 밖의 SQL(LLM 이 쓴 것)은
    행 조회만, 컬럼은 정의 필드, 조인은 승인된 연결만 받는다. 지표 규칙(취소 제외, draft 금지)을 SQL 로 우회하지 못하게 한다.
    정의가 없으면 DB 전체를 보고 집계도 한다. 없는 컬럼은 DB 가 오류로 알려 준다.
    """
    if not model:
        return {"allowed_tables": set(db.tables), "allow_aggregates": True, "columns": None, "edges": None}
    if engine_sql:
        return {"allowed_tables": model.tables(), "allow_aggregates": True, "columns": None, "edges": None}
    table = {d: ds["source"].rpartition(".")[2].lower() for d, ds in model.datasets.items()}
    columns: dict[str, set[str]] = {}
    for d, n in model.fields:
        columns.setdefault(table[d], set()).add(n.lower())
    edges = {table[f]: [{**r, "from": table[r["from"]], "to": table[r["to"]]} for r in rs] for f, rs in model.edges.items()}
    return {"allowed_tables": model.tables(), "allow_aggregates": False, "columns": columns, "edges": edges}


def table_names(allowed: set[str]) -> set[str]:
    """'스키마.테이블' 에서 테이블 이름만. 목록·설명 도구는 기본 스키마의 테이블 이름으로 찾는다."""
    return {a.rpartition(".")[2] for a in allowed}


def _table_allowed(t: exp.Table, allowed: set[str], schema: str | None) -> bool:
    """allowed 의 항목은 '테이블' 또는 '스키마.테이블'(소문자). 접두어 없는 쪽은 기본 스키마(schema)로 본다."""
    if t.catalog:
        return False
    name, db, default = t.name.lower(), t.db.lower(), (schema or "").lower()
    for a in allowed:
        a_db, _, a_name = a.rpartition(".")
        if a_name == name and (db == a_db or (not db and a_db == default) or (not a_db and db == default)):
            return True
    return False


def _real_tables(tree: exp.Expression) -> list[exp.Table]:
    """실제 테이블 참조. CTE 이름을 가리키는 것은 뺀다."""
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return [t for t in tree.find_all(exp.Table) if t.name and not (not t.db and t.name.lower() in ctes)]


def _aliases(tree: exp.Expression) -> dict[str, str]:
    """별칭(없으면 이름) → 실제 테이블 이름(소문자)."""
    return {t.alias_or_name.lower(): t.name.lower() for t in _real_tables(tree)}


def _join_pairs(join: exp.Join, alias: dict[str, str]) -> list[tuple[str, str]]:
    """ON 의 컬럼 = 컬럼 조건을 ('테이블.컬럼', '테이블.컬럼') 로. 별칭은 실제 테이블로 푼다."""
    on = join.args.get("on")
    pairs = []
    for eq in on.find_all(exp.EQ) if on is not None else []:
        if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
            pairs.append(tuple(f"{alias.get(c.table.lower(), c.table.lower())}.{c.name.lower()}" for c in (eq.left, eq.right)))
    return pairs


def _check_joins(tree: exp.Expression, edges: dict[str, list[dict]]) -> None:
    """qualify 를 거친 트리(USING 은 ON 으로 바뀌어 있다)에서 서로 다른 테이블 사이 조인이 정의의 연결과 맞는지 본다.

    ponytail: JOIN ... ON 만 본다. FROM a, b WHERE a.x = b.y 와 CTE·하위 쿼리를 사이에 둔 조인은 거르지 않는다. 필요해지면 WHERE 의 등식도 모은다
    """
    allowed = {
        frozenset(frozenset({f"{r['from']}.{a}".lower(), f"{r['to']}.{b}".lower()}) for a, b in zip(r["from_columns"], r["to_columns"]))
        for rs in edges.values() for r in rs
    }
    alias = _aliases(tree)
    for j in tree.find_all(exp.Join):
        groups: dict[frozenset, set] = {}
        for left, right in _join_pairs(j, alias):
            tables = frozenset({left.split(".")[0], right.split(".")[0]})
            if len(tables) == 2 and tables <= set(alias.values()):
                groups.setdefault(tables, set()).add(frozenset({left, right}))
        for cond in groups.values():
            if frozenset(cond) not in allowed:
                got = ", ".join(" = ".join(sorted(p)) for p in cond)
                ok = ", ".join(sorted(", ".join(" = ".join(sorted(p)) for p in e) for e in allowed))
                raise UnsafeSQL(f"승인되지 않은 연결: {got} (허용: {ok})", kind="unknown_join")


def prepare(
    sql: str, dialect: str, allowed_tables: set[str], max_rows: int, allow_aggregates: bool = True, schema: str | None = None,
    read: str | None = None, columns: dict[str, set[str]] | None = None, edges: dict[str, list[dict]] | None = None,
) -> str:
    """검증을 통과하면 행 수 제한을 보장한 SQL을 대상 문법으로 돌려준다.

    allow_aggregates=False 면 행 조회만 받는다. 집계 함수는 OVER 가 붙어도 거절하고, 순위 함수(ROW_NUMBER 등)는 받는다.
    LIMIT 은 max_rows + 1 을 넘지 않게 덮어쓴다. 한 행을 더 받아 봐야 결과가 잘렸는지 알 수 있다.
    read 는 SQL 을 읽을 문법(없으면 dialect). 중립 문법으로 쓴 SQL 을 대상 문법으로 바꿔 돌려준다.
    columns(테이블 → 컬럼, 소문자)를 주면 모든 컬럼 참조가 그 안에서 풀려야 하고, edges(SemanticModel.edges 모양)를 주면
    테이블 사이 조인 조건이 어느 한 연결과 같아야 한다.
    """
    read = read or dialect
    tree = _parse_single_query(sql, read)
    _check_statement_policy(tree, allow_aggregates)
    _check_tables(tree, allowed_tables, schema, dialect)
    if columns is not None or edges is not None:
        _check_columns_and_joins(tree, columns, edges, schema, read)
    return _cap_limit(tree, max_rows).sql(dialect=dialect)


def _parse_single_query(sql: str, read: str) -> exp.Query:
    try:
        statements = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    except SqlglotError as e:
        raise UnsafeSQL(f"문법 오류: {str(e)[:500]}", kind="syntax") from e
    if len(statements) != 1:
        raise UnsafeSQL("SQL은 한 문장만 허용")
    if not isinstance(statements[0], exp.Query):
        raise UnsafeSQL("조회(SELECT) 문만 허용")
    return statements[0]


def _check_statement_policy(tree: exp.Query, allow_aggregates: bool) -> None:
    """데이터 변경, 집계 정책, 부작용 함수, FROM 절 테이블 함수."""
    if tree.find(*_FORBIDDEN):
        raise UnsafeSQL("데이터 변경 구문이 포함됨")
    if not allow_aggregates and tree.find(exp.AggFunc):
        raise UnsafeSQL("행 조회만 허용. 집계는 지표 정의(t2s compile)로 하고, 맞는 지표가 없으면 지표가 없다고 답한다")
    for f in tree.find_all(exp.Func):
        name = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        if name.startswith(_DENIED_FUNCS):
            raise UnsafeSQL(f"쓸 수 없는 함수: {name}")
        if not allow_aggregates and isinstance(f, exp.Anonymous) and name.startswith(_ANON_AGGS):
            raise UnsafeSQL(f"행 조회만 허용. {name} 은 집계 함수다")
    if any(isinstance(t.this, exp.Func) for t in tree.find_all(exp.Table)):
        raise UnsafeSQL("FROM 절의 테이블 함수는 허용하지 않음")


def _check_tables(tree: exp.Query, allowed: set[str], schema: str | None, dialect: str) -> None:
    unknown = sorted({t.sql(dialect) for t in _real_tables(tree) if not _table_allowed(t, allowed, schema)})
    if unknown:
        raise UnsafeSQL(f"허용되지 않은 테이블: {', '.join(unknown)}", kind="unknown_table")


def _check_columns_and_joins(tree: exp.Query, columns: dict | None, edges: dict | None, schema: str | None, read: str) -> None:
    """별칭·CTE·하위 쿼리·SELECT * 확장은 sqlglot 의 qualify 가 푼다. 돌려줄 SQL 은 바꾸지 않게 사본에 한다."""
    tables = {t: {c: "unknown" for c in cs} for t, cs in (columns or {}).items()}
    try:
        qualified = qualify(tree.copy(), schema={schema: tables} if schema else tables, db=schema, dialect=read,
                            identify=False, validate_qualify_columns=columns is not None)
    except OptimizeError as e:
        raise UnsafeSQL(f"없는 컬럼: {str(e)[:500]}", kind="unknown_column") from e
    if edges is not None:
        _check_joins(qualified, edges)


def _cap_limit(tree: exp.Query, max_rows: int) -> exp.Query:
    """LIMIT·TOP·FETCH FIRST 모두 "limit" 자리에 잡힌다. 숫자로 적힌 작은 LIMIT 만 그대로 둔다."""
    cap = max_rows + 1
    value = tree.args["limit"].args.get("expression") if isinstance(tree.args.get("limit"), exp.Limit) else None
    if isinstance(value, exp.Literal) and value.is_int and int(value.name) <= cap:
        return tree
    return tree.limit(cap)


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _owner(c: exp.Column, alias: dict[str, str], tables: set[str], known: set[str]) -> str | None:
    """컬럼의 실제 테이블. 접두어가 없으면 known('테이블.컬럼')에 맞는 테이블이 하나일 때만 안다."""
    if c.table:
        return alias.get(c.table.lower(), c.table.lower())
    hits = [t for t in tables if f"{t}.{c.name.lower()}" in known]
    return hits[0] if len(hits) == 1 else None


def lint(sql: str, dialect: str, read: str | None = None, time_columns: set[str] | None = None,
         edges: dict[str, list[dict]] | None = None) -> list[dict]:
    """막지는 않는 경고. [{"kind", "message"}]. 읽지 못하는 SQL 이면 빈 목록(거절은 prepare 가 한다).

    date_literal: time_columns('테이블.컬럼', 소문자, 시각이 있는 컬럼)를 날짜만 있는 리터럴과 =, <=, BETWEEN 으로 비교.
    fan_out: X 컬럼을 DISTINCT 없이 SUM·AVG·COUNT 하는데 X 를 한 행 쪽으로 하는 연결(edges, policy 의 모양)로 Y 가 조인됨.
    ponytail: 문장 전체를 한 범위로 본다. 하위 쿼리마다 따로 봐야 하는 오탐이 나오면 scope 별로 나눈다
    """
    try:
        tree = sqlglot.parse_one(sql, read=read or dialect)
        tree = qualify(tree, dialect=read or dialect, identify=False, validate_qualify_columns=False)
    except SqlglotError:
        return []
    alias = _aliases(tree)
    tables = set(alias.values())
    warnings = []
    known = {t.lower() for t in time_columns or ()}

    def time_col(e) -> str | None:
        if isinstance(e, exp.Column) and (t := _owner(e, alias, tables, known)) and f"{t}.{e.name.lower()}" in known:
            return f"{t}.{e.name.lower()}"
        return None

    def date(e) -> dt.date | None:
        try:
            return dt.date.fromisoformat(e.name) if isinstance(e, exp.Literal) and e.is_string and _DATE_ONLY.fullmatch(e.name) else None
        except ValueError:
            return None

    for node in tree.find_all(exp.EQ, exp.LTE, exp.GTE, exp.Between):
        if isinstance(node, exp.Between):
            col, d, op = time_col(node.this), date(node.args.get("high")), "BETWEEN"
        elif isinstance(node, exp.EQ):
            col, d = time_col(node.left) or time_col(node.right), date(node.right) or date(node.left)
            op = "="
        else:  # col <= 'd' 와 'd' >= col 은 같다
            col, d = (node.left, node.right) if isinstance(node, exp.LTE) else (node.right, node.left)
            col, d, op = time_col(col), date(d), "<="
        if col and d:
            start = d if op == "=" else "시작일"
            warnings.append({"kind": "date_literal", "message": (
                f"{col} 는 시각이 있는 컬럼인데 날짜 '{d}' 와 {op} 로 비교해 그날 시각이 있는 행이 빠진다. "
                f"반열림 구간으로 쓴다: {col} >= '{start}' AND {col} < '{d + dt.timedelta(days=1)}'")})

    joined = {frozenset(p) for j in tree.find_all(exp.Join) for p in _join_pairs(j, alias)}
    for agg in tree.find_all(exp.Sum, exp.Avg, exp.Count):
        if agg.find(exp.Distinct):
            continue
        for x in {_owner(c, alias, tables, set()) for c in agg.find_all(exp.Column)} - {None}:
            for r in (r for rs in (edges or {}).values() for r in rs if r["to"].lower() == x):
                y = r["from"].lower()
                pairs = {frozenset({f"{y}.{a}".lower(), f"{x}.{b}".lower()}) for a, b in zip(r["from_columns"], r["to_columns"])}
                if y != x and pairs <= joined:
                    warnings.append({"kind": "fan_out", "message": (
                        f"{agg.sql(dialect)}: {x} 한 행이 {y} 행 수만큼 반복돼 값이 부푼다. "
                        f"{x} 기준으로 먼저 집계하거나 COUNT(DISTINCT {x} 키)를 쓴다")})
    return warnings


def shape(sql: str, dialect: str, read: str | None = None) -> dict:
    """검증과 별개로 SQL 구조를 돌려준다. 별칭은 테이블 이름으로 푼다. 파싱하지 못하면 빈 값.

    ponytail: 스키마 없이 qualify 하므로 USING 의 왼쪽은 바로 앞 테이블로 보고, 접두어 없는 컬럼은 그대로 둔다
    """
    empty = {"tables": [], "joins": [], "columns": [], "aggregates": [], "filters": None, "group_by": [], "order_by": [], "limit": None}
    try:
        tree = sqlglot.parse_one(sql, read=read or dialect)
    except SqlglotError:
        return empty
    if tree is None:
        return empty
    try:
        tree = qualify(tree, dialect=read or dialect, identify=False, validate_qualify_columns=False)
    except SqlglotError:
        pass
    alias = _aliases(tree)
    cols = {f"{alias.get(c.table.lower(), c.table.lower())}.{c.name.lower()}" if c.table else c.name.lower() for c in tree.find_all(exp.Column)}
    where, limit = tree.args.get("where"), tree.args.get("limit")
    value = limit.args.get("expression") if isinstance(limit, exp.Limit) else None
    return {
        "tables": sorted(set(alias.values())),
        "joins": [{"left": a, "right": b} for j in tree.find_all(exp.Join) for a, b in _join_pairs(j, alias)],
        "columns": sorted(cols),
        "aggregates": sorted({f.sql_name() for f in tree.find_all(exp.AggFunc)}),
        "filters": where.this.sql(dialect) if where else None,
        "group_by": [g.sql(dialect) for g in tree.args["group"].expressions] if tree.args.get("group") else [],
        "order_by": [o.sql(dialect) for o in tree.args["order"].expressions] if tree.args.get("order") else [],
        "limit": int(value.name) if isinstance(value, exp.Literal) and value.is_int else None,
    }
