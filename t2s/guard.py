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
from sqlglot.optimizer.scope import Scope, traverse_scope

from .engine import source_table

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
    """kind: syntax, unknown_table, unknown_column, policy(질문의 뜻을 바꿔야 하는 거절). 정의와 다른 조인·컬럼은 거절하지 않고 lint 가 알린다."""

    def __init__(self, message: str, kind: str = "policy"):
        super().__init__(message)
        self.kind = kind


def policy(db, model, engine_sql: bool = False) -> dict:
    """prepare 에 넘길 허용 범위. t2s 의 validate·run·values·sql·compile 이 같이 쓴다. model 은 SemanticModel 이다(t2s 는 정의 없이 돌지 않는다).

    SQL 은 LLM 이 자유롭게 쓴다. 막는 것은 안전(읽기 전용·한 문장·위험 함수·행 수)뿐이다.
    테이블은 정의의 테이블과 DB(기본 스키마)의 테이블 모두 받는다. 집계도 받는다. 정의 밖 테이블·컬럼, 승인 안 된 연결,
    정의 안 된 집계는 lint·CLI 가 경고로 알려 답에 밝히게 한다.
    columns 는 정의 필드(정의 밖인지 가르는 기준)다. CLI 는 실제 DB 컬럼으로 바꿔 prepare 에 넘긴다(없는 컬럼만 잡는다).
    """
    tables = model.tables() | (set(db.tables) if db is not None else set())
    if engine_sql:
        return {"allowed_tables": tables, "allow_aggregates": True, "columns": None}
    # 조회문 source 데이터셋은 실제 테이블이 아니라 LLM 이 쓴 SQL 에서 이름으로 부를 수 없다. 컬럼 범위에서 뺀다
    table = _physical(model)
    columns: dict[str, set[str]] = {}
    for d, n in model.fields:
        if d in table:
            columns.setdefault(table[d], set()).add(n.lower())
    return {"allowed_tables": tables, "allow_aggregates": True, "columns": columns}


def _physical(model) -> dict[str, str]:
    """데이터셋 → 실제 테이블 이름(소문자, 스키마 뺌). 조회문 source 데이터셋은 없다."""
    return {d: t.rpartition(".")[2] for d, ds in model.datasets.items() if (t := source_table(ds))}


def relationships(model) -> dict[str, dict[str, list[dict]]]:
    """LLM 이 실제 테이블 이름으로 쓴 SQL 을 대조할 연결. edges 는 승인된 연결, drafts 는 승인 전 연결(SemanticModel.edges 모양)."""
    table = _physical(model)

    def rename(rels: dict[str, list[dict]]) -> dict[str, list[dict]]:
        return {table[f]: [{**r, "from": table[r["from"]], "to": table[r["to"]]} for r in rs if r["to"] in table]
                for f, rs in rels.items() if f in table}
    drafts: dict[str, list[dict]] = {}
    for r in model.draft_edges:
        drafts.setdefault(r["from"], []).append(r)
    return {"edges": rename(model.edges), "drafts": rename(drafts)}


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


def _origin(scope: Scope, source: str, column: str, depth: int = 0) -> str | None:
    """scope 에서 source.column 이 가리키는 실제 '테이블.컬럼'(소문자). WITH·하위 쿼리는 그 안의 SELECT 로 거슬러 간다.

    컬럼을 그대로(이름만 바꿔) 내보낸 것만 따라간다. 계산한 값·UNION·못 찾는 이름이면 None.
    상관 하위 쿼리가 바깥 질의의 표를 가리키면 바깥 범위(scope.parent)에서 찾는다.
    """
    while source not in scope.sources and scope.parent is not None:
        scope = scope.parent
    src = scope.sources.get(source)
    if isinstance(src, exp.Table):
        return f"{src.name.lower()}.{column.lower()}"
    if not isinstance(src, Scope) or not isinstance(src.expression, exp.Select) or depth > 20:
        return None
    for proj in src.expression.selects:
        if proj.alias_or_name.lower() == column.lower():
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Column) and inner.table:
                return _origin(src, inner.table, inner.name, depth + 1)
            return None
    return None


def _end(scope: Scope, c: exp.Column) -> str | None:
    """조인 조건 한쪽 컬럼의 원래 '테이블.컬럼'. 계산한 값·못 찾는 이름이면 None."""
    return _origin(scope, c.table, c.name) if c.table else None


def _pairs(rels: dict[str, list[dict]] | None) -> dict[frozenset, str]:
    """연결마다 키 등식 묶음 → 연결 이름."""
    return {frozenset(frozenset({f"{r['from']}.{a}".lower(), f"{r['to']}.{b}".lower()}) for a, b in zip(r["from_columns"], r["to_columns"])):
            r.get("name", "")
            for rs in (rels or {}).values() for r in rs}


class _Joins:
    """조인 조건을 정의의 연결과 대조해 맞지 않는 것을 경고로 모은다. 막지 않는다(SQL 은 LLM 이 자유롭게 쓴다).

    승인된 연결은 조용히 지나가고, 승인 전 연결·정의에 없는 연결·키 등식이 아닌 조인은 결과를 확인하고 답에 밝히라고 알린다."""

    def __init__(self, edges: dict | None, drafts: dict | None):
        self.approved, self.drafts = _pairs(edges), _pairs(drafts)
        self.warnings: list[dict] = []

    def _warn(self, message: str) -> None:
        item = {"kind": "unapproved_join", "message": message}
        if item not in self.warnings:
            self.warnings.append(item)

    def shape(self, text: str) -> None:
        self._warn(f"조인 조건이 키 컬럼 등식이 아니다: {text}. 행이 크게 늘거나 빠질 수 있다. 결과 행 수를 확인하고 답에 이 조건으로 붙였다고 밝힌다")

    def untraced(self, text: str) -> None:
        self._warn(f"조인 키가 어느 테이블의 원래 컬럼인지 거슬러 찾지 못함: {text}. 계산하거나 감싼 값으로 이으면 정의의 연결과 대조할 수 없다. "
                   "결과를 확인하고 답에 밝힌다")

    def match(self, groups: dict[frozenset, set]) -> None:
        for cond in groups.values():
            key = frozenset(cond)
            if key in self.approved:
                continue
            got = ", ".join(" = ".join(sorted(p)) for p in cond)
            if key in self.drafts:
                self._warn(f"승인 전 연결 {self.drafts[key]}({got})로 붙였다. 아직 검토 중인 연결이라 결과를 확인하고 답에 밝힌다")
            else:
                self._warn(f"정의에 없는 연결({got})로 붙였다. 키가 한 행 쪽에서 유일한지 확인되지 않아 행이 늘 수 있다. "
                           "결과를 확인하고 답에 밝힌다. 자주 쓰면 정의에 연결 추가를 제안한다")


def _add(groups: dict[frozenset, set], ends: list[str]) -> None:
    tables = frozenset(e.split(".")[0] for e in ends)
    if len(tables) == 2:  # 같은 테이블끼리의 등식은 연결이 아니다
        groups.setdefault(tables, set()).add(frozenset(ends))


def _own(node: exp.Expression, sel: exp.Expression) -> bool:
    """node 가 sel 블록 자신의 조건인지(더 안쪽 하위 쿼리의 것이 아닌지)."""
    return node.find_ancestor(exp.Select) is sel


_COMPARE = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _branches(q: exp.Expression) -> list[exp.Select]:
    """하위 쿼리의 SELECT 들. UNION·INTERSECT·EXCEPT 는 갈래마다."""
    if isinstance(q, exp.Subquery):
        return _branches(q.this)
    if isinstance(q, exp.SetOperation):
        return _branches(q.left) + _branches(q.right)
    return [q] if isinstance(q, exp.Select) else []


def _side(scope: Scope, e: exp.Expression) -> tuple[str, str | None]:
    """하위 쿼리와 비교하는 한쪽 값. ('col', '테이블.컬럼') 은 원래 컬럼 그대로, ('value', None) 은 컬럼이 없거나 집계한 값,
    ('computed', None) 은 컬럼을 감싸거나 계산해 원래 컬럼으로 거슬러 가지 못하는 값."""
    if isinstance(e, exp.Column):
        found = _origin(scope, e.table, e.name) if e.table else None
        return ("col", found) if found else ("computed", None)
    if not any(True for _ in e.find_all(exp.Column)) or e.find(exp.AggFunc):
        return ("value", None)
    return ("computed", None)


def _check_correlated(scope: Scope, sel: exp.Select, joins: _Joins) -> None:
    """상관 하위 쿼리의 WHERE. 바깥 표 컬럼과 안쪽 표 컬럼을 함께 읽는 조건은 '안쪽 컬럼 = 바깥 컬럼' 그대로인지,
    그 등식들이 승인된 연결(복합 키 포함)과 맞는지 본다. 바깥 표만 읽는 조건(o.status = 'x')은 연결이 아니라 그대로 둔다."""
    where = sel.args.get("where")
    if scope.parent is None or where is None:
        return
    conds = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
    groups: dict[frozenset, set] = {}
    for cond in conds:
        cols = [c for c in cond.find_all(exp.Column) if c.table and _own(c, sel)]
        outer = [c for c in cols if c.table not in scope.sources]
        if not outer or len(outer) == len(cols):
            continue
        if not (isinstance(cond, exp.EQ) and isinstance(cond.left, exp.Column) and isinstance(cond.right, exp.Column)):
            joins.shape(cond.sql())
            continue
        ends = [_end(scope, cond.left), _end(scope, cond.right)]
        if None in ends:
            joins.untraced(cond.sql())
        else:
            _add(groups, ends)
    joins.match(groups)


def _check_subquery_compare(scope: Scope, sel: exp.Select, by_select: dict[int, Scope], joins: _Joins) -> None:
    """값을 하위 쿼리 결과와 비교하는 곳: x IN (SELECT …), x = ANY (SELECT …), x = (SELECT …), (a, b) IN (SELECT …, …).
    양쪽이 다른 표의 원래 컬럼이고 같다(=·IN)로 비교하면 반조인이라 정의의 연결과 대조한다. 한쪽이 값(상수·집계)이면 필터라 보지 않는다.
    같다로 비교하는데 한쪽 키를 감싸거나 계산했으면 연결과 대조할 수 없다고 알린다. <·> 같은 비교는 값 비교로 본다."""
    for sub in sel.find_all(exp.Subquery):
        if not _own(sub, sel):
            continue
        op = sub.parent.parent if isinstance(sub.parent, (exp.Any, exp.All)) else sub.parent
        if isinstance(op, exp.In) and op.args.get("query") is sub:
            left, equal = op.this, True
        elif isinstance(op, _COMPARE):
            right_side = op.right is sub or op.right is sub.parent
            left, equal = (op.left if right_side else op.right), isinstance(op, exp.EQ)
        else:
            continue  # EXISTS, FROM 의 파생 테이블 등
        if not equal:
            continue
        lefts = left.expressions if isinstance(left, exp.Tuple) else [left]
        for branch in _branches(sub.this):
            inner = by_select.get(id(branch))
            picked = branch.selects
            if inner is None or len(picked) != len(lefts):
                continue  # 열 수가 다르면 DB 가 오류로 알린다
            groups: dict[frozenset, set] = {}
            for lft, proj in zip(lefts, picked):
                a = _side(scope, lft)
                b = _side(inner, proj.this if isinstance(proj, exp.Alias) else proj)
                if "value" in (a[0], b[0]):
                    continue
                if a[0] != "col" or b[0] != "col":
                    joins.untraced(op.sql())
                    continue
                _add(groups, [a[1], b[1]])
            joins.match(groups)


def _check_joins(tree: exp.Expression, edges: dict[str, list[dict]] | None, drafts: dict[str, list[dict]] | None = None) -> list[dict]:
    """qualify 를 거친 트리(USING 은 ON 으로 바뀌어 있다)에서 서로 다른 테이블을 잇는 조건을 정의의 연결과 대조해 경고를 돌려준다.

    보는 곳은 세 가지다.
    - JOIN ... ON 의 컬럼 = 컬럼. ON 이 없는 조인(콤마 조인 FROM a, b 와 CROSS JOIN 포함)과 OR 가 든 ON 은 키 등식이 아니라고 알린다.
    - 상관 하위 쿼리(EXISTS·NOT EXISTS·스칼라 하위 쿼리 등)의 WHERE 에서 바깥 표와 안쪽 표를 함께 읽는 조건(_check_correlated).
    - 값을 하위 쿼리 결과와 같다로 비교하는 곳: IN·= ANY·= (SELECT …)·튜플 IN, UNION 갈래마다(_check_subquery_compare).
    WITH·하위 쿼리 결과를 잇는 조건도 원래 테이블 컬럼으로 거슬러 본다.
    ponytail: ON 은 컬럼 = 컬럼 등식만 대조한다. 함께 쓴 다른 조건(함수로 감싼 등식 등)은 따지지 않는다
    """
    joins = _Joins(edges, drafts)
    scopes = traverse_scope(tree)
    by_select = {id(s.expression): s for s in scopes}
    for scope in scopes:
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            continue
        for j in sel.args.get("joins") or []:
            on = j.args.get("on")
            if on is None or on.find(exp.Or):
                joins.shape(j.sql())
                continue
            groups: dict[frozenset, set] = {}
            for eq in on.find_all(exp.EQ):
                if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
                    ends = [_end(scope, c) for c in (eq.left, eq.right)]
                    if None in ends:
                        joins.untraced(eq.sql())
                    else:
                        _add(groups, ends)
            if not groups:
                joins.shape(j.sql())
            joins.match(groups)
        _check_correlated(scope, sel, joins)
        _check_subquery_compare(scope, sel, by_select, joins)
    return joins.warnings


def dataset_join_warnings(sql: str, read: str, model) -> list[dict]:
    """t2s sql 의 원문(FROM 에 데이터셋 이름)의 조인을 정의의 연결과 대조한 경고. 막지 않는다.

    t2s sql 은 데이터셋을 하위 쿼리로 펼친 SQL 을 실행하므로, 펼치기 전 데이터셋 이름 공간에서 연결을 대조한다.
    """
    try:
        tree = _parse_single_query(sql, read)
    except UnsafeSQL:
        return []
    schema: dict[str, dict[str, str]] = {d.lower(): {} for d in model.datasets}
    for d, n in model.fields:
        schema[d.lower()][n.lower()] = "unknown"
    for key, m in model.metrics.items():  # AGG(별칭.측정) 의 측정도 그 데이터셋 컬럼으로 둔다
        if m.get("_dataset"):
            schema[m["_dataset"].lower()][key.split(".", 1)[1].lower()] = "unknown"
    drafts: dict[str, list[dict]] = {}
    for r in model.draft_edges:
        drafts.setdefault(r["from"], []).append(r)
    try:
        return _check_joins(_qualified(tree, schema, read), model.edges, drafts)
    except SqlglotError:  # 경고는 덤이다. 읽지 못하면 실행 단계가 오류로 알린다
        return []


def _qualified(tree: exp.Expression, tables: dict, read: str, schema: str | None = None) -> exp.Expression:
    """별칭·CTE·하위 쿼리·SELECT * 를 sqlglot qualify 로 푼 사본. 정의에 없는 컬럼이 섞여 풀리지 않으면 별칭이 붙은 것만 푼다."""
    try:
        return qualify(tree.copy(), schema={schema: tables} if schema else tables, db=schema, dialect=read,
                       identify=False, validate_qualify_columns=False)
    except OptimizeError:
        return qualify(tree.copy(), dialect=read, identify=False, validate_qualify_columns=False)


def prepare(
    sql: str, dialect: str, allowed_tables: set[str], max_rows: int, allow_aggregates: bool = True, schema: str | None = None,
    read: str | None = None, columns: dict[str, set[str]] | None = None,
) -> str:
    """검증을 통과하면 행 수 제한을 보장한 SQL을 대상 문법으로 돌려준다. 안전과 정책만 막고, 정의와 다른 쓰임은 lint 가 알린다.

    allow_aggregates=False 면 행 조회만 받는다. 집계 함수는 OVER 가 붙어도 거절하고, 순위 함수(ROW_NUMBER 등)는 받는다.
    LIMIT 은 max_rows + 1 을 넘지 않게 덮어쓴다. 한 행을 더 받아 봐야 결과가 잘렸는지 알 수 있다.
    read 는 SQL 을 읽을 문법(없으면 dialect). 중립 문법으로 쓴 SQL 을 대상 문법으로 바꿔 돌려준다.
    columns(테이블 → 실제로 있는 컬럼, 소문자)를 주면 모든 컬럼 참조가 그 안에서 풀려야 한다(없는 컬럼은 unknown_column).
    정의 필드인지는 보지 않는다.
    """
    read = read or dialect
    tree = _parse_single_query(sql, read)
    _check_statement_policy(tree, allow_aggregates)
    _check_tables(tree, allowed_tables, schema, dialect)
    if columns is not None:
        tables = {t: {c: "unknown" for c in cs} for t, cs in columns.items()}
        try:  # 별칭·CTE·하위 쿼리·SELECT * 확장은 qualify 가 푼다. 돌려줄 SQL 은 바꾸지 않게 사본에 한다
            qualify(tree.copy(), schema={schema: tables} if schema else tables, db=schema, dialect=read,
                    identify=False, validate_qualify_columns=True)
        except OptimizeError as e:
            raise UnsafeSQL(f"없는 컬럼: {str(e)[:500]}", kind="unknown_column") from e
    if dialect == "sqlite":
        _materialize_shared(tree)
    return _cap_limit(tree, max_rows).sql(dialect=dialect)


def _materialize_shared(tree: exp.Expression) -> None:
    """SQLite 에서 두 번 이상 읽는 CTE 에 AS MATERIALIZED 를 붙인다. 결과는 같고 계산 방식만 바뀐다.
    SQLite 는 이런 CTE 를 하위 쿼리로 펼쳐 다른 CTE 의 행마다 원본 표를 다시 읽는 계획을 고르기도 한다(평가셋 bcds-002 는 20초를 넘겼고
    MATERIALIZED 를 붙이면 1초 안에 끝났다). 쓴 사람이 MATERIALIZED·NOT MATERIALIZED 를 정했거나 재귀 CTE 면 두지 않는다."""
    for w in tree.find_all(exp.With):
        if w.args.get("recursive"):
            continue
        for cte in w.expressions:
            uses = sum(1 for t in tree.find_all(exp.Table) if not t.db and t.name == cte.alias)
            if uses >= 2 and cte.args.get("materialized") is None:
                cte.set("materialized", True)


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
        raise UnsafeSQL("행 조회만 허용. 승인 지표는 t2s compile로 계산하고, 정의에 없는 집계는 t2s sql로 계산한다")
    for f in tree.find_all(exp.Func):
        name = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        if name.startswith(_DENIED_FUNCS):
            raise UnsafeSQL(f"쓸 수 없는 함수: {name}")
        if not allow_aggregates and isinstance(f, exp.Anonymous) and name.startswith(_ANON_AGGS):
            raise UnsafeSQL(f"행 조회만 허용. {name}은 집계 함수다. 집계는 t2s compile 또는 t2s sql을 쓴다")
    if any(isinstance(t.this, exp.Func) for t in tree.find_all(exp.Table)):
        raise UnsafeSQL("FROM 절의 테이블 함수는 허용하지 않음")


def _check_tables(tree: exp.Query, allowed: set[str], schema: str | None, dialect: str) -> None:
    unknown = sorted({t.sql(dialect) for t in _real_tables(tree) if not _table_allowed(t, allowed, schema)})
    if unknown:
        raise UnsafeSQL(f"허용되지 않은 테이블: {', '.join(unknown)}", kind="unknown_table")


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
         edges: dict[str, list[dict]] | None = None, drafts: dict[str, list[dict]] | None = None,
         columns: dict[str, set[str]] | None = None, joins: bool = False,
         defined: dict[str, set[str]] | None = None) -> list[dict]:
    """막지는 않는 경고. [{"kind", "message"}]. 읽지 못하는 SQL 이면 빈 목록(거절은 prepare 가 한다).

    date_literal: time_columns('테이블.컬럼', 소문자, 시각이 있는 컬럼)를 날짜만 있는 리터럴과 =, <=, BETWEEN 으로 비교.
    fan_out: X 컬럼을 DISTINCT 없이 SUM·AVG·COUNT 하는데 X 를 한 행 쪽으로 하는 연결(edges, relationships 의 모양)로 Y 가 조인됨.
    unapproved_join(joins=True): 승인된 연결(edges)이 아닌 조인. 승인 전 연결(drafts)은 이름을 알려 준다.
    outside_definition(defined 를 주면): 정의 테이블에서 정의 필드(defined, 테이블 → 필드)가 아닌 컬럼을 읽음.
    columns(테이블 → 실제 컬럼)는 접두어 없는 컬럼·WITH·하위 쿼리를 원래 테이블로 푸는 데 쓴다.
    ponytail: 문장 전체를 한 범위로 본다. 하위 쿼리마다 따로 봐야 하는 오탐이 나오면 scope 별로 나눈다
    """
    read = read or dialect
    try:
        parsed = sqlglot.parse_one(sql, read=read)
        tree = _qualified(parsed, {t: {c: "unknown" for c in cs} for t, cs in (columns or {}).items()}, read)
    except SqlglotError:
        return []
    alias = _aliases(tree)
    tables = set(alias.values())
    warnings = []
    if joins:
        try:
            warnings += _check_joins(tree, edges, drafts)
        except SqlglotError:
            pass
    if defined is not None:
        other = sorted(tables - set(defined))
        if other:
            warnings.append({"kind": "outside_definition", "message": (
                f"정의에 없는 테이블을 읽었다: {', '.join(other)}. 뜻·키가 정의로 확인되지 않았다. "
                "t2s schema 로 구조를 보고 결과를 확인한 뒤 답에 정의 밖 테이블을 썼다고 밝힌다. 자주 쓰면 정의에 데이터셋 추가를 제안한다")})
        outside = sorted({f"{t}.{c.name.lower()}" for c in tree.find_all(exp.Column)
                          if c.table and (t := alias.get(c.table.lower())) in defined and c.name.lower() not in defined[t]})
        if outside:
            warnings.append({"kind": "outside_definition", "message": (
                f"정의에 필드로 없는 컬럼을 읽었다: {', '.join(outside)}. 뜻·단위·값의 범위가 정의로 확인되지 않았다. "
                "값을 확인하고(t2s values) 답에 정의 밖 컬럼을 썼다고 밝힌다. 자주 쓰면 정의에 필드 추가를 제안한다")})
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
