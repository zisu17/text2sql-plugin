"""2층 관계형 질의(Ossie core-spec/relational_semantics.md): 사람이나 LLM 이 쓴 SQL 의 AGG(측정)를 계산한다.

- 정의의 데이터셋은 SQL 의 표다. FROM 에 데이터셋 이름(또는 모델이름.데이터셋)을 쓰면 그 데이터셋 필드가 컬럼인 표가 된다.
- 데이터셋 지표는 측정이다. AGG(별칭.지표) 로만 계산한다. 모델 지표는 2층 측정이 아니다(10.3절).
- AGG 는 측정의 행(기본키)마다 한 번만 센다. 조인으로 행이 늘어도 부풀지 않는다(8절 G2).
  그 질의 블록의 FROM·WHERE 결과를 CTE 로 두고, 묶음 값이 같은 행 중 기본키마다 첫 행만 세어 측정의 집계를 계산한다.
  행을 한 번만 읽는다. 결과는 규격 9절 참고 전략(묶음마다 상관 하위 쿼리)과 같다. 원래 질의의 조인·조건·묶음·정렬은 바꾸지 않는다.
- CTE·하위 쿼리가 측정을 그대로 넘기면(SELECT sum_sal AS payroll) 기본키와 필드를 숨은 컬럼으로 함께 넘긴다(7.7절).
- 정의를 거치지 않은 집계(SUM(e.sal) 등)도 계산하되 undefined 에 적는다(정의 안 된 계산).
"""
from __future__ import annotations

from itertools import product

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

from .engine import BUILD, SemanticModel, _date_diff_days, _outer_aggregates
from .ossie import EngineError, status

_DATASET = "t2s_dataset"  # 데이터셋 표로 바꾼 SELECT 의 meta 표시


def rewrite(sql: str, model: SemanticModel, read: str = "duckdb", dialect: str = "sqlite", allow_draft: bool = False,
            physical: set[str] | None = None) -> dict:
    """AGG 를 계산하는 SQL 로 바꾼다. {"sql", "measures": 쓴 측정, "undefined": 정의 안 된 집계}.

    physical(소문자 테이블 이름)은 FROM 에 그대로 둘 정의 밖 DB 테이블이다. 데이터셋의 원본 테이블은 넣지 않는다
    (그 테이블은 데이터셋 이름으로 써야 AGG 로 지표를 계산할 수 있다)."""
    try:
        trees = sqlglot.parse(sql, read=read)
    except SqlglotError as err:
        raise EngineError("invalid_sql", f"SQL 을 읽을 수 없음 ({str(err)[:200]})") from err
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise EngineError("invalid_sql", "SELECT 문 하나만 받는다")
    tree = trees[0]
    simplified = _unwrap(tree, read, dialect)
    undefined = [n.sql(read) for n in tree.find_all(exp.AggFunc) if not isinstance(n, exp.Agg)]
    _relations(tree, model, physical or set())
    r = _Rewriter(model, tree, allow_draft, dialect)
    for scope in build_scope(tree).traverse():  # 안쪽 블록부터
        r.block(scope)
    r.attach()
    return {"sql": tree.sql(dialect, pretty=True), "measures": list(dict.fromkeys(r.used)), "undefined": undefined,
            "simplified": simplified}


def _unwrap(tree: exp.Expression, read: str, dialect: str) -> list[str]:
    """묶음 안의 단일 AGG를 감싼 MIN·MAX를 풀고 SQLite AVG의 실수 반환형을 보존한다.
    윈도 집계는 여러 묶음에 적용되므로 FILTER가 있어도 그대로 둔다."""
    done = []
    for agg in list(tree.find_all(exp.Agg)):
        if isinstance(agg.parent, exp.Filter) and agg.parent.this is agg:
            raise EngineError(
                "invalid_agg", "AGG(...)에 FILTER를 직접 붙일 수 없음",
                hint="대상 행을 WHERE로 거른 뒤 AGG로 계산한다. 기간별 조건이 다르면 각 기간을 별도로 묶어 계산한 뒤 결과를 합친다",
            )
        outer = agg.parent
        while outer is not None and not isinstance(outer, (exp.AggFunc, exp.Select)):
            outer = outer.parent
        if not isinstance(outer, exp.AggFunc):
            continue
        wrapper = outer.parent if isinstance(outer.parent, exp.Filter) else outer
        if isinstance(wrapper.parent, exp.Window):
            continue
        if isinstance(outer, (exp.Avg, exp.Min, exp.Max)) and outer.this is agg and not isinstance(outer.parent, exp.Filter):
            if isinstance(outer, exp.Avg):
                if dialect != "sqlite":
                    raise EngineError("invalid_agg", "AVG의 반환형을 보존하려면 하위 쿼리에서 AGG 값을 구한 뒤 AVG를 적용한다")
                replacement = exp.cast(agg.copy(), "DOUBLE")
            else:
                replacement = agg
            done.append(outer.sql(read))
            outer.replace(replacement)
            continue
        raise EngineError("invalid_agg", f"집계 함수 안에 AGG 를 쓸 수 없음: {outer.sql(read)}",
                          hint="AGG(x) 는 이미 묶음마다 집계한 값이라 AGG(x) 로 그대로 쓴다. 묶음끼리 다시 집계하려면 "
                               "하위 쿼리에서 묶음별 AGG 값을 낸 뒤 바깥 질의에서 집계한다")
    return done


def _relations(tree: exp.Expression, model: SemanticModel, physical: set[str]) -> None:
    """FROM 의 데이터셋 이름을 그 데이터셋의 필드 표(하위 쿼리)로 바꾼다. CTE 이름은 건드리지 않는다."""
    names = {name.lower(): name for name in model.datasets}
    for scope in build_scope(tree).traverse():
        for alias, src in list(scope.sources.items()):
            if not isinstance(src, exp.Table):
                continue
            # sqlglot 은 WITH RECURSIVE 가 없어도 SQLite 에서 허용되는 자기 참조 CTE를
            # Table 로 남긴다. 자기 CTE 본문 안에서만 그 이름을 건너뛴다.
            parent = src.parent
            while parent and not (isinstance(parent, exp.CTE) and parent.alias_or_name.lower() == src.name.lower()):
                parent = parent.parent
            if not src.db and not src.catalog and parent:
                continue
            d = names.get(src.name.lower()) if not src.catalog and (not src.db or src.db.lower() == model.name.lower()) else None
            if d is None and not src.catalog and src.name.lower() in physical:
                continue  # 정의 밖 DB 테이블은 그대로 읽는다. CLI 가 정의 밖이라고 알린다
            if d is None:
                raise EngineError("unknown_relation", f"FROM 에는 데이터셋이나 정의 밖 DB 테이블을 쓴다. 정의된 데이터셋의 원본 테이블은 데이터셋 이름으로 쓴다: {src.sql()}",
                                  sorted(model.datasets))
            fields = [n for dd, n in model.fields if dd == d]
            sel = sqlglot.parse_one(f"SELECT {', '.join(f'{model._field(d, n).sql(BUILD)} AS {n}' for n in fields)} FROM {model._table(d)}", read=BUILD)
            sel.meta[_DATASET] = d
            src.replace(sel.subquery(alias))


class _Rewriter:
    def __init__(self, model: SemanticModel, tree: exp.Expression, allow_draft: bool, dialect: str):
        self.model, self.tree, self.allow_draft, self.dialect = model, tree, allow_draft, dialect
        self.ctes: list[tuple[exp.CTE | None, exp.CTE]] = []  # (이 CTE 앞에 넣는다. None 이면 맨 끝, 넣을 CTE)
        self.passed: dict[tuple[int, str], tuple] = {}  # (SELECT, 컬럼) → 넘긴 측정
        self.used: list[str] = []

    def block(self, scope: Scope) -> None:
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            return
        aggs = [n for n in sel.find_all(exp.Agg) if n.find_ancestor(exp.Select) is sel]
        if not aggs:
            return
        name = f"t2s_j{len(self.ctes)}"
        _qualify(scope, sel)
        groups, sets, clause = _groups(sel, name)
        measures = []  # AGG 마다 (지표 키, 기본키 컬럼 이름, {필드: 컬럼 이름}, 넘길 (이름, 식) 목록)
        for u, agg in enumerate(aggs):
            col = agg.this
            if not isinstance(col, exp.Column):
                raise EngineError("invalid_agg", f"AGG 에는 측정 컬럼 하나만 쓴다: {agg.sql()}", hint="AGG(a + b) 대신 AGG(a) + AGG(b) 로 쓴다")
            key, keys, fields = self._measure(scope, col.table, col.name)
            ks = [f"t2s_u{u}_k{i}" for i in range(len(keys))]
            fs = {f: f"t2s_u{u}_{f}" for f in fields}
            measures.append((key, ks, fs, [*zip(ks, keys), *((fs[f], e) for f, e in fields.items())]))
        j = sel.copy()
        for k in ("with_", "distinct", "group", "having", "qualify", "windows", "order", "limit", "offset", "into", "locks"):
            j.set(k, None)
        carried = self._one_pass(sel, aggs, measures, groups, sets, clause, name)
        j.set("expressions", [exp.alias_(g.copy(), f"t2s_g{i}") for i, g in enumerate(groups)] + [exp.alias_(e.copy(), n) for n, e in carried])
        self.ctes.append((self._container(sel), exp.CTE(this=j, alias=exp.TableAlias(this=exp.to_identifier(name)))))

    def _one_pass(self, sel: exp.Select, aggs: list, measures: list, groups: list, sets: list[frozenset[int]], clause: exp.Group | None,
                  name: str) -> list[tuple[str, exp.Expression]]:
        """행을 한 번만 읽는 계산. 결과는 규격 9절 참고 전략(묶음마다 상관 하위 쿼리)과 같고, 묶음이 많아도 느려지지 않는다.
        CTE 에 측정마다 '같은 묶음·같은 기본키의 첫 행' 번호를 창 함수로 붙인다. 바깥 질의는 그 CTE 를 묶고, AGG 는 지표 식의 집계마다
        FILTER (WHERE 첫 행) 을 붙여 계산한다. 첫 행만 세므로 조인으로 늘어난 행은 한 번만 들어가고, 바깥 조인이 채운 빈 행(키가 NULL)은
        빠진다. 묶음에 측정 행이 하나도 없으면 집계의 빈 입력 값(COUNT 0)이 된다.
        ROLLUP·CUBE·GROUPING SETS 는 한 행이 여러 묶음에 든다. 묶음 모음마다 번호를 따로 붙이고, GROUPING() 으로 그 행의 묶음 모음을 골라 쓴다."""
        carried = []
        varying = sorted(set().union(*sets) - set.intersection(*map(set, sets)))  # 묶음 모음마다 들고 나는 묶음 식
        for u, (agg, (key, ks, fs, pairs)) in enumerate(zip(aggs, measures)):
            carried += pairs
            keys = [e for n, e in pairs if n in ks]
            values = []
            for s, members in enumerate(sets):
                number = exp.Window(this=exp.RowNumber(), partition_by=[groups[i].copy() for i in sorted(members)] + [k.copy() for k in keys])
                carried.append((f"t2s_u{u}_n{s}", number))
                first = exp.and_(exp.column(f"t2s_u{u}_n{s}", table=name).eq(1), *(exp.column(k, table=name).is_(exp.null()).not_() for k in ks))
                values.append(self._counted(key, fs, name, first))
            if len(values) > 1:
                which = [exp.and_(*(exp.func("GROUPING", exp.column(f"t2s_g{i}", table=name)).eq(int(i not in m)) for i in varying)) for m in sets]
                values = [exp.Case(ifs=[exp.If(this=w, true=v) for w, v in zip(which[:-1], values[:-1])], default=values[-1])]
            agg.replace(values[0])
        # 바깥 질의는 CTE 를 읽는다. 묶음 식은 묶음 컬럼으로, 그 밖의 컬럼(정의 안 된 집계의 인자 등)은 CTE 로 넘긴 컬럼으로 바꾼다.
        # 하위 쿼리 안에서는 바깥 표를 가리키는 컬럼만 바꾼다. 하위 쿼리 자신의 표를 읽는 컬럼은 그대로 둔다
        aliases = {s.alias for s in sel.selects if isinstance(s, exp.Alias)}
        outer = _sources(sel)
        moved: dict[str, tuple[str, exp.Expression]] = {}

        def move(e: exp.Expression, alias_ok: bool) -> exp.Expression:
            def swap(n):
                inner = _inner_sources(n)
                if inner is not None:
                    cols = [n] if isinstance(n, exp.Column) else list(n.find_all(exp.Column))
                    if not cols or any(not c.table or c.table not in outer or c.table in inner for c in cols):
                        return n
                for i, g in enumerate(groups):
                    if n == g:
                        return exp.column(f"t2s_g{i}", table=name)
                if isinstance(n, exp.Column) and n.table != name and not (alias_ok and inner is None and not n.table and n.name in aliases):
                    moved.setdefault(n.sql(), (f"t2s_c{len(moved)}", n.copy()))
                    return exp.column(moved[n.sql()][0], table=name)
                return n
            return e.transform(swap)

        selects = []
        for s in sel.selects:
            m = move(s, False)
            selects.append(m if isinstance(s, exp.Alias) or not s.alias_or_name or m.alias_or_name == s.alias_or_name else exp.alias_(m, s.alias_or_name))
        sel.set("expressions", selects)
        for k in ("having", "order"):
            if sel.args.get(k):
                sel.set(k, move(sel.args[k], True))
        sel.set("group", clause)
        for k in ("joins", "laterals", "where"):
            sel.set(k, None)
        sel.set("from_", exp.From(this=exp.to_table(name)))
        return carried + list(moved.values())

    def _counted(self, key: str, fs: dict[str, str], table: str, first: exp.Expression) -> exp.Expression:
        """지표 식. 필드는 CTE 로 넘긴 컬럼으로 바꾸고, 바깥 집계마다 FILTER (WHERE first) 를 붙인다(지표의 FILTER 와는 AND)."""
        value = self.model._metric(key, True)[1].transform(lambda n: exp.column(fs[n.name], table=table) if isinstance(n, exp.Column) else n)
        for a in _outer_aggregates(value):
            if isinstance(a, exp.Filter):
                a.set("expression", exp.Where(this=exp.and_(a.expression.this, first.copy())))
            elif a is value:
                value = exp.Filter(this=a, expression=exp.Where(this=first.copy()))
            else:
                a.replace(exp.Filter(this=a.copy(), expression=exp.Where(this=first.copy())))
        return value.transform(lambda node: _date_diff_days(node, self.dialect))

    def _measure(self, scope: Scope, alias: str, name: str) -> tuple[str, list[exp.Expression], dict[str, exp.Expression]]:
        """scope 에서 alias.name 이 가리키는 측정. (지표 키, 기본키 식들, {필드: 식})."""
        if alias:
            found = [(alias, scope.sources.get(alias))]
            if found[0][1] is None:
                raise EngineError("unknown_relation", f"FROM 에 없는 별칭: {alias}", sorted(scope.sources))
        else:
            found = [(a, s) for a, s in scope.sources.items() if self._provides(s, name)]
            if len(found) != 1:
                raise EngineError("unknown_measure", f"측정 {name} 을 가진 표를 하나로 정할 수 없음", [a for a, _ in found] or sorted(scope.sources),
                                  hint="AGG(별칭.측정) 처럼 표 별칭을 붙인다")
        alias, src = found[0]
        sel = src.expression if isinstance(src, Scope) else None
        d = sel.meta.get(_DATASET) if sel is not None else None
        if d:
            key = f"{d}.{name}"
            if key not in self.model.metrics:
                raise EngineError("unknown_measure", f"{d} 에 측정 {name} 이 없음", [k.split(".")[1] for k in self.model.metrics if k.startswith(d + ".")])
            if not self.allow_draft and status(self.model.metrics[key]) != "approved":
                raise EngineError("metric_not_approved", f"{key} 는 {status(self.model.metrics[key])} 상태라 쓸 수 없음")
            pk = self.model.datasets[d].get("primary_key") or []
            if not pk:
                raise EngineError("no_row_identity", f"{d} 에 primary_key 가 없어 측정을 행마다 한 번 셀 수 없음", hint="데이터셋에 primary_key 를 정의한다")
            fields = sorted({c.name for c in self.model._metric(key, True)[1].find_all(exp.Column)})
            self.used.append(key)
            return key, [exp.column(p, table=alias) for p in pk], {f: exp.column(f, table=alias) for f in fields}
        if not isinstance(sel, exp.Select) or sel.args.get("group") or sel.args.get("distinct"):
            raise EngineError("unknown_measure", f"{alias}.{name} 은 측정이 아님", hint="측정은 데이터셋 표나, 측정을 그대로 넘기는 CTE·하위 쿼리에서만 AGG 로 쓴다")
        memo = (id(sel), name)
        if memo not in self.passed:  # CTE·하위 쿼리가 측정을 그대로 넘긴다
            proj = next((p for p in sel.selects if p.alias_or_name == name), None)
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if not isinstance(inner, exp.Column):
                raise EngineError("unknown_measure", f"{alias}.{name} 은 측정이 아님")
            key, keys, fields = self._measure(src, inner.table, inner.name)
            for i, k in enumerate(keys):
                sel.append("expressions", exp.alias_(k, f"t2s_{name}_k{i}"))
            for f, e in fields.items():
                sel.append("expressions", exp.alias_(e, f"t2s_{name}_{f}"))
            proj.replace(exp.alias_(exp.null(), name))  # 측정 자리는 값이 없다. AGG 로만 읽는다
            self.passed[memo] = (key, len(keys), list(fields))
        key, n, fields = self.passed[memo]
        return key, [exp.column(f"t2s_{name}_k{i}", table=alias) for i in range(n)], {f: exp.column(f"t2s_{name}_{f}", table=alias) for f in fields}

    def _provides(self, src, name: str) -> bool:
        sel = src.expression if isinstance(src, Scope) else None
        if not isinstance(sel, exp.Select):
            return False
        d = sel.meta.get(_DATASET)
        return f"{d}.{name}" in self.model.metrics if d else any(p.alias_or_name == name for p in sel.selects)

    def _container(self, node: exp.Expression) -> exp.CTE | None:
        """node 를 품은 맨 바깥 WITH 의 CTE. 블록의 CTE 는 그 CTE 앞에 둔다(뒤의 CTE 만 앞의 CTE 를 읽는다)."""
        top = self.tree.args.get("with_")
        cte = node.find_ancestor(exp.CTE)
        while cte is not None and cte.parent is not top:
            cte = cte.find_ancestor(exp.CTE)
        return cte

    def attach(self) -> None:
        if not self.ctes:
            return
        top = self.tree.args.get("with_") or exp.With(expressions=[])
        items = list(top.expressions)
        for before, cte in self.ctes:
            at = next((i for i, x in enumerate(items) if x is before), len(items))
            items.insert(at, cte)
        top.set("expressions", items)
        self.tree.set("with_", top)


def _qualify(scope: Scope, sel: exp.Select) -> None:
    """이 블록의 표 이름 없는 컬럼에 표 별칭을 붙인다. 그 이름을 가진 표가 하나뿐일 때만 붙인다. 묶음 식 deptno 와 하위 쿼리의
    emp.deptno 를 같은 묶음 컬럼으로 바꾸기 위해서다. GROUP BY·HAVING·ORDER BY 에서 SELECT 별칭인 이름은 별칭이라 건드리지 않는다."""
    known = {}
    for alias, (_, src) in scope.selected_sources.items():
        sub = src.expression if isinstance(src, Scope) else None
        known[alias] = {p.alias_or_name for p in sub.selects} if isinstance(sub, exp.Select) else None  # None: DB 표라 컬럼을 모른다
    aliases = {s.alias for s in sel.selects if isinstance(s, exp.Alias)}
    for col in list(sel.find_all(exp.Column)):
        if col.table or col.find_ancestor(exp.Select) is not sel or isinstance(col.this, exp.Star):
            continue
        if col.name in aliases and col.find_ancestor(exp.Group, exp.Having, exp.Order) is not None:
            continue
        found = [a for a, cols in known.items() if cols is None or col.name in cols]
        if len(found) == 1 and (len(known) == 1 or known[found[0]] is not None):
            col.set("table", exp.to_identifier(found[0]))


def _sources(sel: exp.Select) -> set[str]:
    """SELECT 의 FROM·JOIN 표 별칭."""
    tables = [sel.args["from_"].this] if sel.args.get("from_") else []
    return {t.alias_or_name for t in tables + [j.this for j in sel.args.get("joins") or []]}


def _inner_sources(n: exp.Expression) -> set[str] | None:
    """n 이 바깥 블록의 하위 쿼리 안에 있으면 그 사이 SELECT 들의 표 별칭, 아니면 None."""
    found, p = None, n.parent
    while p is not None:
        if isinstance(p, exp.Select):
            found = (found or set()) | _sources(p)
        p = p.parent
    return found


def _groups(sel: exp.Select, table: str) -> tuple[list[exp.Expression], list[frozenset[int]], exp.Group | None]:
    """(묶음 식, 묶음 모음 목록, 바깥 질의의 GROUP BY). 묶음 모음은 묶음 식 번호 집합이다. 보통 GROUP BY 는 모음이 하나고,
    ROLLUP·CUBE·GROUPING SETS 는 여럿이다. 위치 번호와 SELECT 별칭은 그 식으로, GROUP BY ALL 은 집계 없는 SELECT 식으로 푼다.
    바깥 GROUP BY 는 원래 모양 그대로 묶음 식만 CTE 의 묶음 컬럼(table.t2s_g번호)으로 바꾼 것이다."""
    group = sel.args.get("group")
    if not group:
        return [], [frozenset()], None
    selects = [s.this if isinstance(s, exp.Alias) else s for s in sel.selects]
    aliases = {s.alias: s.this for s in sel.selects if isinstance(s, exp.Alias)}
    out: list[exp.Expression] = []

    def index(e: exp.Expression) -> int:
        e = e.unnest()  # GROUPING SETS ((a), b) 의 (a) 는 a 다
        if isinstance(e, exp.Literal) and e.is_int:
            e = selects[int(e.this) - 1]
        elif isinstance(e, exp.Column) and not e.table and e.name in aliases:
            e = aliases[e.name]
        hit = next((i for i, o in enumerate(out) if e == o), None)
        if hit is None:
            out.append(e)
        return len(out) - 1 if hit is None else hit

    def items(t: exp.Expression) -> frozenset[int]:
        t = t.unnest()
        return frozenset(index(x) for x in (t.expressions if isinstance(t, exp.Tuple) else [t]))

    def sets(e: exp.Expression) -> list[frozenset[int]]:
        if isinstance(e, exp.Rollup):  # ROLLUP(a, b) = (a, b), (a), ()
            parts = [items(t) for t in e.expressions]
            return [frozenset().union(*parts[:n]) for n in range(len(parts), -1, -1)]
        if isinstance(e, exp.Cube):  # CUBE(a, b) = 모든 부분집합
            parts = [items(t) for t in e.expressions]
            return [frozenset().union(*(p for p, on in zip(parts, mask) if on)) for mask in product((True, False), repeat=len(parts))]
        if isinstance(e, exp.GroupingSets):
            return [s for t in e.expressions for s in (sets(t) if isinstance(t, (exp.Rollup, exp.Cube, exp.GroupingSets)) else [items(t)])]
        return [items(e)]

    def column(e: exp.Expression) -> exp.Expression:
        if isinstance(e, (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple)):
            return e.__class__(expressions=[column(x) for x in e.expressions])
        return exp.column(f"t2s_g{index(e)}", table=table)

    if group.args.get("all"):
        out = [s for s in selects if not s.find(exp.AggFunc)]
        return out, [frozenset(range(len(out)))], exp.Group(expressions=[exp.column(f"t2s_g{i}", table=table) for i in range(len(out))])
    combos = [frozenset()]
    for e in group.expressions:  # GROUP BY 항목끼리는 곱한다: GROUP BY x, ROLLUP(a) = (x, a), (x)
        combos = [a | b for a in combos for b in sets(e)]
    if not any(isinstance(e, (exp.Rollup, exp.Cube, exp.GroupingSets)) for e in group.expressions):
        return out, combos[:1], exp.Group(expressions=[exp.column(f"t2s_g{i}", table=table) for i in range(len(out))])
    return out, list(dict.fromkeys(combos)), exp.Group(expressions=[column(e) for e in group.expressions])
