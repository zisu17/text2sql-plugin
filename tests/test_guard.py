"""가드의 컬럼·연결 검사가 골든셋 대조 SQL 을 막지 않는지, shape 가 구조를 읽는지."""
import pytest
import yaml

from tests.conftest import GOLDEN
from t2s.guard import UnsafeSQL, lint, policy, prepare, shape

GOLD = [g for g in yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) if g.get("sql")]


def _columns(model):
    cols = {}
    for d, n in model.fields:
        cols.setdefault(d, set()).add(n.lower())
    return cols


@pytest.mark.parametrize("gold", GOLD, ids=lambda g: str(g["id"]))
def test_gold_sql_passes(model, gold):
    prepare(gold["sql"], "sqlite", model.tables(), 100, allow_aggregates=True, columns=_columns(model), edges=model.edges)


def test_anonymous_aggregates_rejected(model):
    """sqlglot 이 AggFunc 로 보지 않는 집계(TOTAL, EVERY, JSONB_AGG)도 행 조회 정책에서 막는다."""
    kw = dict(columns=_columns(model), edges=model.edges, allow_aggregates=False)
    for sql, dialect in (("SELECT TOTAL(price) FROM order_items", "sqlite"), ("SELECT EVERY(price > 0) FROM order_items", "postgres"),
                         ("SELECT JSONB_AGG(price) FROM order_items", "postgres")):
        with pytest.raises(UnsafeSQL):
            prepare(sql, dialect, model.tables(), 100, **kw)
    prepare("SELECT price FROM order_items", "sqlite", model.tables(), 100, **kw)


def test_column_and_join_rejected(model):
    kw = dict(columns=_columns(model), edges=model.edges)
    with pytest.raises(UnsafeSQL) as e:
        prepare("SELECT o.nope FROM orders o", "sqlite", model.tables(), 100, **kw)
    assert e.value.kind == "unknown_column"
    with pytest.raises(UnsafeSQL) as e:
        prepare("SELECT o.order_id FROM orders o JOIN products p ON o.order_id = p.product_id", "sqlite", model.tables(), 100, **kw)
    assert e.value.kind == "unknown_join" and "order_items.order_id = orders.order_id" in str(e.value)


def test_shape():
    s = shape(next(g["sql"] for g in GOLD if g["id"] == 6), "sqlite")
    assert s["tables"] == ["order_items", "orders", "products"]
    assert len(s["joins"]) == 2 and s["aggregates"] == ["SUM"] and s["limit"] == 1
    assert "order_items.price" in s["columns"] and s["filters"]


def test_lint(model):
    times = {"orders.order_purchase_timestamp"}
    edges = policy(None, model)["edges"]

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
