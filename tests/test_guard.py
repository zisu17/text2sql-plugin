"""가드는 안전·정책만 막고, 정의와 다른 조인·컬럼은 경고로 알리는지. shape 가 구조를 읽는지."""
import pytest
import yaml

from tests.conftest import GOLDEN
from t2s.guard import UnsafeSQL, lint, policy, prepare, relationships, shape

GOLD = [g for g in yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) if g.get("sql")]


def _columns(model):
    cols = {}
    for d, n in model.fields:
        cols.setdefault(d, set()).add(n.lower())
    return cols


@pytest.mark.parametrize("gold", GOLD, ids=lambda g: str(g["id"]))
def test_gold_sql_passes(model, gold):
    # 지표 엔진이 만든 SQL 은 실제 호출에서도 컬럼·연결 검사를 다시 하지 않는다.
    rules = {} if gold["path"] == "metric" else {"columns": _columns(model)}
    prepare(gold["sql"], "sqlite", model.tables(), 100, allow_aggregates=True, **rules)


def test_anonymous_aggregates_rejected(model):
    """sqlglot 이 AggFunc 로 보지 않는 집계(TOTAL, EVERY, JSONB_AGG)도 행 조회 정책에서 막는다."""
    kw = dict(columns=_columns(model), allow_aggregates=False)
    for sql, dialect in (("SELECT TOTAL(price) FROM order_items", "sqlite"), ("SELECT EVERY(price > 0) FROM order_items", "postgres"),
                         ("SELECT JSONB_AGG(price) FROM order_items", "postgres")):
        with pytest.raises(UnsafeSQL):
            prepare(sql, dialect, model.tables(), 100, **kw)
    prepare("SELECT price FROM order_items", "sqlite", model.tables(), 100, **kw)


def _join_warnings(model, sql):
    """LLM 이 쓴 SQL(실제 테이블 이름)의 조인 경고. CLI 의 run·validate 와 같은 입력이다."""
    rel = relationships(model)
    found = lint(sql, "sqlite", edges=rel["edges"], drafts=rel["drafts"], columns=_columns(model), joins=True)
    return [w["message"] for w in found if w["kind"] == "unapproved_join"]


def test_unknown_column_rejected_but_join_only_warned(model):
    kw = dict(columns=_columns(model))
    with pytest.raises(UnsafeSQL) as e:
        prepare("SELECT o.nope FROM orders o", "sqlite", model.tables(), 100, **kw)
    assert e.value.kind == "unknown_column"
    sql = "SELECT o.order_id FROM orders o JOIN products p ON o.order_id = p.product_id"
    prepare(sql, "sqlite", model.tables(), 100, **kw)  # 조인은 막지 않는다
    assert any("정의에 없는 연결(orders.order_id = products.product_id)" in m for m in _join_warnings(model, sql))
    draft = "SELECT s.seller_state FROM order_items i JOIN sellers s ON i.seller_id = s.seller_id"
    assert any("승인 전 연결 order_items_to_sellers" in m for m in _join_warnings(model, draft))


@pytest.mark.parametrize("sql", [
    "SELECT o.order_id FROM orders o JOIN customers c ON 1=1",
    "SELECT o.order_id FROM orders o JOIN customers c ON o.customer_id = c.customer_id OR 1=1",
])
def test_join_without_key_condition_is_warned(model, sql):
    prepare(sql, "sqlite", model.tables(), 100, columns=_columns(model))
    assert any("키 컬럼 등식이 아니다" in m for m in _join_warnings(model, sql))


def test_outside_definition_column_is_warned(model):
    columns = {"orders": {"order_id", "order_status"}}
    warnings = lint("SELECT o.order_id, o.internal_note FROM orders o", "sqlite", defined=columns)
    assert [w["kind"] for w in warnings] == ["outside_definition"] and "orders.internal_note" in warnings[0]["message"]
    assert lint("SELECT o.order_id FROM orders o", "sqlite", defined=columns) == []


def test_shape():
    s = shape(next(g["sql"] for g in GOLD if g["id"] == 6), "sqlite")
    assert s["tables"] == ["order_items", "orders", "products"]
    assert len(s["joins"]) == 2 and s["aggregates"] == ["SUM"] and s["limit"] == 1
    assert "order_items.price" in s["columns"] and s["filters"]


def test_lint(model):
    times = {"orders.order_purchase_timestamp"}
    edges = relationships(model)["edges"]

    def kinds(sql):
        return [w["kind"] for w in lint(sql, "sqlite", time_columns=times, edges=edges)]

    ts = "o.order_purchase_timestamp"
    for cond in (f"{ts} = '2018-01-05'", f"{ts} <= '2018-01-31'", f"'2018-01-31' >= {ts}", f"{ts} BETWEEN '2018-01-01' AND '2018-01-31'"):
        assert kinds(f"SELECT o.order_id FROM orders o WHERE {cond}") == ["date_literal"], cond
    for cond in (f"{ts} >= '2018-01-05' AND {ts} < '2018-01-06'", f"{ts} = '2018-01-05 10:00:00'", "o.order_status = '2018-01-05'"):
        assert kinds(f"SELECT o.order_id FROM orders o WHERE {cond}") == [], cond

    join = " FROM orders o JOIN order_items i ON i.order_id = o.order_id"
    assert kinds("SELECT COUNT(o.order_id)" + join) == ["fan_out"]  # 주문 한 행이 품목 수만큼 반복된다
    assert kinds("SELECT COUNT(DISTINCT o.order_id)" + join) == []
    assert kinds("SELECT SUM(i.price)" + join) == []  # 품목은 N 쪽이라 부풀지 않는다
    assert lint("SELEC nope", "sqlite") == []


@pytest.mark.parametrize("sql, ok", [
    ("WITH t AS (SELECT order_id, customer_id FROM orders) SELECT t.order_id, c.customer_state FROM t JOIN customers c ON t.customer_id = c.customer_id", True),
    ("WITH t AS (SELECT customer_id AS cid FROM orders) SELECT c.customer_state FROM t JOIN customers c ON t.cid = c.customer_id", True),
    ("WITH t AS (SELECT order_id FROM orders) SELECT p.product_category_name FROM t JOIN products p ON t.order_id = p.product_id", False),
    ("WITH a AS (SELECT order_id FROM orders), b AS (SELECT order_id AS k FROM a) SELECT p.product_id FROM b JOIN products p ON b.k = p.product_id", False),
    ("SELECT p.product_id FROM (SELECT order_id FROM orders) s JOIN products p ON s.order_id = p.product_id", False),
    ("WITH t AS (SELECT order_id || 'x' AS k FROM orders) SELECT p.product_id FROM t JOIN products p ON t.k = p.product_id", False),
    # 첫 연결이 틀리고 둘째가 맞아도 거절한다(JOIN 마다 따로 본다)
    ("SELECT o.order_id FROM orders o JOIN products p ON o.order_id = p.product_id JOIN customers c ON o.customer_id = c.customer_id", False),
])
def test_join_through_cte_or_subquery(model, sql, ok):
    """WITH·하위 쿼리 결과를 이을 때도 원래 테이블 컬럼으로 거슬러 승인된 연결인지 본다. 아니면 막지 않고 경고한다."""
    prepare(sql, "sqlite", model.tables(), 100, columns=_columns(model))
    assert (_join_warnings(model, sql) == []) is ok


@pytest.mark.parametrize("sql, ok", [
    # 하위 쿼리로 잇는 조인도 승인된 연결과 대조한다(아니면 경고)
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.product_id = o.order_id)", False),
    ("SELECT order_id FROM orders WHERE customer_id IN (SELECT seller_id FROM order_items)", False),
    ("SELECT o.order_id, (SELECT i.price FROM order_items i WHERE i.product_id = o.order_id LIMIT 1) AS p FROM orders o", False),
    ("SELECT o.order_id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.product_id = o.order_id)", False),
    ("SELECT order_id FROM orders WHERE customer_id = ANY (SELECT seller_id FROM order_items)", False),
    ("SELECT o.order_id FROM orders o WHERE o.order_id IN (SELECT i.order_id || 'x' FROM order_items i)", False),  # 계산한 값
    ("SELECT o.order_id FROM orders o WHERE o.customer_id IN (SELECT c.customer_id FROM customers c WHERE c.customer_state IN ('SP', 'RJ'))", True),
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND i.price > 100)", True),
    ("SELECT o.order_id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)", True),
    ("SELECT order_id FROM orders WHERE order_id IN (SELECT order_id FROM order_items)", True),
    ("SELECT o.order_id, (SELECT i.price FROM order_items i WHERE i.order_id = o.order_id LIMIT 1) AS p FROM orders o", True),
    ("SELECT o.order_id FROM orders o WHERE o.order_status IN ('delivered', 'shipped')", True),  # 값 목록은 연결이 아니다
    ("SELECT a.order_id FROM orders a WHERE EXISTS (SELECT 1 FROM orders b WHERE b.customer_id = a.customer_id)", True),  # 같은 표
    # 감싸거나 비교·OR 로 이어도 경고를 건너뛰지 못한다
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE CAST(i.product_id AS TEXT) = o.order_id)", False),
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE LOWER(i.product_id) = LOWER(o.order_id))", False),
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.product_id > o.order_id)", False),
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id OR i.price > 100)", False),
    ("SELECT o.order_id FROM orders o WHERE LOWER(o.customer_id) IN (SELECT seller_id FROM order_items)", False),
    ("SELECT o.order_id FROM orders o WHERE o.customer_id IN (SELECT seller_id FROM order_items UNION SELECT seller_id FROM order_items)", False),
    ("SELECT o.order_id FROM orders o WHERE (o.customer_id, o.order_id) IN (SELECT seller_id, order_id FROM order_items)", False),
    ("SELECT o.order_id FROM orders o WHERE o.order_id = (SELECT i.product_id FROM order_items i LIMIT 1)", False),
    ("SELECT o.order_id FROM orders o WHERE (SELECT i.product_id FROM order_items i LIMIT 1) = o.order_id", False),
    # 바깥 표만 읽는 조건, 승인 키 UNION, 상수를 고르는 하위 쿼리는 연결이 아니거나 승인된 연결이다
    ("SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND o.order_status = 'delivered')", True),
    ("SELECT o.order_id FROM orders o WHERE o.order_id IN (SELECT i.order_id FROM order_items i UNION SELECT r.order_id FROM order_reviews r)", True),
    ("SELECT o.order_id FROM orders o WHERE o.order_id NOT IN (SELECT 'x')", True),
    ("SELECT o.order_id FROM orders o WHERE o.customer_id = (SELECT c.customer_id FROM customers c WHERE c.customer_state = 'SP' LIMIT 1)", True),
    # 콤마 조인과 CROSS JOIN 은 ON 이 없어 키 등식이 아니라고 알린다
    ("SELECT o.order_id FROM orders o, customers c WHERE o.customer_id = c.customer_id", False),
    ("SELECT o.order_id FROM orders o CROSS JOIN customers c", False),
])
def test_subquery_joins_follow_relationships(model, sql, ok):
    prepare(sql, "sqlite", max_rows=100, **policy(None, model))  # 막지 않는다
    assert (_join_warnings(model, sql) == []) is ok


def test_sqlite_materializes_shared_ctes():
    """SQLite 에서 두 번 이상 읽는 CTE 는 AS MATERIALIZED 로 실행한다(결과는 같다). 한 번 읽거나 쓴 사람이 정했으면 그대로 둔다."""
    tables = {"orders", "customers"}
    shared = ("WITH h AS (SELECT o.order_id, c.customer_unique_id AS uid FROM orders o JOIN customers c ON c.customer_id = o.customer_id), "
              "f AS (SELECT uid, COUNT(*) AS n FROM h GROUP BY uid) SELECT h.order_id, f.n FROM h JOIN f ON f.uid = h.uid")
    out = prepare(shared, "sqlite", tables, 10)
    assert "h AS MATERIALIZED (" in out and "f AS (" in out
    assert "MATERIALIZED" not in prepare(shared, "postgres", tables, 10)
    kept = prepare(shared.replace("WITH h AS (", "WITH h AS NOT MATERIALIZED ("), "sqlite", tables, 10)
    assert "h AS NOT MATERIALIZED (" in kept
    once = "WITH h AS (SELECT order_id FROM orders) SELECT order_id FROM h"
    assert "MATERIALIZED" not in prepare(once, "sqlite", tables, 10)
