"""엔진 점검: Olist 모양의 작은 데이터를 SQLite 메모리에 올려 결과 숫자와 거절 규칙을 확인한다.

python -m pytest tests/test_engine.py
"""

import pytest
import yaml

from tests.conftest import GOLDEN, MODEL, make_db
from t2s.engine import EngineError, SemanticModel, derive


def rows(con, model, *args, **kw):
    return sorted(con.execute(model.compile(*args, dialect="sqlite", **kw)).fetchall())


def test_numbers(con, model):
    assert rows(con, model, ["revenue"], ["products.product_category_name"]) == [("beleza", 130), ("esporte", 70)]
    assert rows(con, model, ["revenue"], ["products.product_category_name"], ["customers.customer_state = 'SP'"]) == [
        ("beleza", 100),
        ("esporte", 70),
    ]
    # 매출은 order_items, 주문 수는 orders 기준으로 따로 집계한 뒤 합친다
    assert rows(con, model, ["revenue", "order_count"], ["customers.customer_state"]) == [("RJ", 30, 1), ("SP", 170, 2)]
    monthly = rows(con, model, ["order_count"], ["orders.order_purchase_timestamp__month"])
    assert [(str(d)[:7], n) for d, n in monthly] == [("2018-01", 2), ("2018-02", 1)]
    assert rows(con, model, ["avg_delivery_days"], allow_draft=True) == [(7.5,)]
    assert rows(con, model, ["revenue", "freight"]) == [(200, 20)]  # 같은 기준 테이블이라 한 번에 집계
    # 주문 수는 COUNT(기본키)라 주문상품 기준으로 세도 부풀지 않는다(o1 은 두 카테고리에 한 번씩)
    assert rows(con, model, ["order_count"], ["products.product_category_name"]) == [("beleza", 2), ("esporte", 2)]


def _same(got: list, want: list) -> bool:
    """숫자는 소수 둘째 자리까지, 문자열은 앞부분('2018-01' ≈ '2018-01-01')으로 비교한다."""
    if not want:  # 결과 없음: 행이 없거나 집계가 NULL 한 행
        return all(v is None for r in got for v in r)

    def cell(g, w):
        return round(float(g), 2) == round(float(w), 2) if isinstance(w, (int, float)) else str(g).startswith(str(w))

    return len(got) == len(want) and all(len(g) == len(w) and all(map(cell, g, w)) for g, w in zip(got, want))


def test_golden_answers(con):
    """문항 파일의 정답이 대조 SQL 결과와 같은지 본다. 문항 파일 자체가 틀리지 않았는지 확인한다."""
    for item in yaml.safe_load(GOLDEN.read_text(encoding="utf-8")):
        if item.get("sql"):
            assert _same(con.execute(item["sql"]).fetchall(), item["answer"]), item["id"]


def test_golden_engine(con, model):
    """문항의 지표 호출값(call)을 그대로 넘기면 엔진이 정답을 내는지 본다."""
    items = [i for i in yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) if i.get("call")]
    assert items
    for item in items:
        sql = model.compile(dialect="sqlite", **item["call"])
        if item.get("expr"):  # 지표 조합은 expr 로 SQL 안에서 계산한다
            sql = derive(sql, item["expr"], item["call"].get("dimensions", []), item["call"]["metrics"])
        got = con.execute(sql).fetchall()
        assert _same(got if item["call"].get("order_by") else sorted(got, key=str), item["answer"]), item["id"]


def test_other_dialect(model):
    sql = model.compile(["revenue", "order_count"], ["customers.customer_state"], dialect="postgres")
    assert "FULL OUTER JOIN" in sql and "IS NOT DISTINCT FROM" in sql


APPROVED = [{"vendor_name": "TEXT2SQL", "data": '{"status": "approved"}'}]


def _doc():
    return yaml.safe_load(MODEL.read_text(encoding="utf-8"))


def _code(model, *args, **kw):
    with pytest.raises(EngineError) as e:
        model.compile(*args, **kw)
    return e.value.code


def test_injection_blocked(model):
    """where·정의 식으로 함수·하위 쿼리가 들어가 정의 밖 데이터를 읽던 경로."""
    for where in [
        "POSITION('x' IN query_to_xml('SELECT pw FROM secret', TRUE, FALSE, '')::TEXT) > 0",
        "pg_sleep(2) IS NOT NULL",
        "LENGTH(customers.customer_state) > 1",
        "1 = 1",  # 필드가 없는 조건
    ]:
        assert _code(model, ["revenue"], where=[where]) == "invalid_where", where
    assert model.compile(["revenue"], where=["orders.order_status IN ('delivered') AND orders.order_purchase_timestamp >= '2018-01-01'"])

    doc = _doc()
    customers = next(d for d in doc["datasets"] if d["name"] == "customers")
    customers["fields"][2]["expression"]["sql"] = "(SELECT card_no FROM payments_pii LIMIT 1)"
    assert _code(SemanticModel(doc), ["revenue"], ["customers.customer_state"]) == "invalid_field"
    customers["fields"][2]["expression"]["sql"] = "pg_sleep(1)"
    assert _code(SemanticModel(doc), ["revenue"], ["customers.customer_state"]) == "invalid_field"

    doc = _doc()
    next(d for d in doc["datasets"] if d["name"] == "customers")["source"] = "(SELECT * FROM payments_pii)"
    assert _code(SemanticModel(doc), ["revenue"], ["customers.customer_state"]) == "invalid_source"

    doc = _doc()
    doc["metrics"].append({"name": "raw_price", "expression": {"sql": "order_items.price"}, "custom_extensions": APPROVED})
    doc["metrics"].append({"name": "sneaky", "expression": {"sql": "MAX(query_to_xml('SELECT 1', TRUE, FALSE, ''))"}, "custom_extensions": APPROVED})
    assert _code(SemanticModel(doc), ["raw_price"]) == "invalid_metric"  # 집계 없는 지표
    assert _code(SemanticModel(doc), ["sneaky"]) == "invalid_metric"


def test_guard_blocks_hidden_sql():
    """허용 테이블 검사를 문자열 안 SQL 로 우회하던 경로(PostgreSQL query_to_xml 등)와 부작용 함수."""
    from t2s.guard import UnsafeSQL, prepare

    allowed = {"orders"}
    for sql in [
        "SELECT query_to_xml('SELECT card_no FROM payments_pii', TRUE, FALSE, '') FROM orders",
        "SELECT pg_sleep(2) FROM orders",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT * INTO stolen FROM orders",
    ]:
        dialect = "duckdb" if "read_csv" in sql else "postgres"
        with pytest.raises(UnsafeSQL):
            prepare(sql, dialect, allowed, 10)
    # 한 행 더(11) 받아야 잘렸는지 안다. 큰 LIMIT 은 덮어쓰고 작은 LIMIT 은 그대로 둔다
    assert prepare("SELECT order_id, UPPER(order_status) FROM orders", "postgres", allowed, 10).endswith("LIMIT 11")
    assert prepare("SELECT * FROM orders LIMIT 1000000000", "postgres", allowed, 10).endswith("LIMIT 11")
    assert prepare("SELECT * FROM orders LIMIT 3", "postgres", allowed, 10).endswith("LIMIT 3")
    # 스키마 접두어: 기본 스키마(olist) 밖의 같은 이름 테이블은 거절
    assert prepare("SELECT * FROM olist.orders", "postgres", allowed, 10, schema="olist")
    with pytest.raises(UnsafeSQL):
        prepare("SELECT * FROM secret_schema.orders", "postgres", allowed, 10, schema="olist")
    with pytest.raises(UnsafeSQL):
        prepare("SELECT * FROM orders", "postgres", {"olist.orders"}, 10)  # 정의는 olist.orders 인데 기본 스키마가 다름
    assert prepare("SELECT * FROM orders", "postgres", {"olist.orders"}, 10, schema="olist")


def test_home_is_value_table(con):
    """조건에만 쓴 테이블(order_items)이 기준이 되어 orders 값 합계가 부풀던 경우(1206 이어야 할 값이 1371)."""
    doc = _doc()
    orders = next(d for d in doc["datasets"] if d["name"] == "orders")
    orders["fields"].append({"name": "one", "expression": {"sql": "1"}, "datatype": "Integer"})
    doc["metrics"] += [
        {"name": "big_item_sum", "expression": {"sql": "SUM(orders.one) FILTER (WHERE order_items.price >= 30)"}, "custom_extensions": APPROVED},
        {"name": "big_item_case", "expression": {"sql": "SUM(CASE WHEN order_items.price >= 30 THEN orders.one END)"}, "custom_extensions": APPROVED},
        {"name": "big_item_orders", "expression": {"sql": "COUNT(orders.order_id) FILTER (WHERE order_items.price >= 30)"}, "custom_extensions": APPROVED},
    ]
    m = SemanticModel(doc)
    assert _code(m, ["big_item_sum"]) == "fan_out_risk"
    assert _code(m, ["big_item_case"]) == "fan_out_risk"
    # COUNT(기본키)는 COUNT(DISTINCT)로 바뀌어 주문상품 기준으로 세도 정확하다: 30 이상 상품이 있는 주문 o1, o2, o3
    assert rows(con, m, ["big_item_orders"]) == [(3,)]
    assert rows(con, m, ["revenue"]) == [(200,)]  # 값과 조건이 같은 방향인 기존 지표는 그대로


def test_joins_and_dates():
    """키가 아닌 연결로 값이 3배가 되던 경우, 데이터셋 나열 순서에 따라 행이 빠지던 경우, DATEDIFF 가 DB 마다 다르던 경우."""
    con = make_db()
    con.executescript("""
        CREATE TABLE geolocation (geolocation_id INTEGER PRIMARY KEY, zip TEXT, state TEXT);
        INSERT INTO geolocation VALUES (1, '01', 'SP'), (2, '01', 'SP'), (3, '01', 'SP'), (4, '02', 'RJ');
        INSERT INTO orders (order_id, customer_id, order_status, order_purchase_timestamp, order_delivered_customer_date) VALUES ('o5', 'c2', 'shipped', '2018-03-01 23:00:00', NULL),
                                  ('o6', 'c2', 'delivered', '2018-03-01 23:00:00', '2018-03-02 01:00:00'),
                                  ('o7', 'c2', 'delivered', '2018-03-01 10:00:00', '2018-03-04 22:00:00');
    """)
    doc = _doc()
    customers = next(d for d in doc["datasets"] if d["name"] == "customers")
    customers["fields"].append({"name": "zip", "expression": {"sql": "SUBSTR(customer_id, 2, 1)"}, "datatype": "String"})
    doc["datasets"].append({"name": "geolocation", "source": "geolocation", "primary_key": ["geolocation_id"], "fields": [
        {"name": "geolocation_id", "expression": {"sql": "geolocation_id"}}, {"name": "zip", "expression": {"sql": "zip"}},
        {"name": "state", "expression": {"sql": "state"}}]})
    doc["relationships"].append({"name": "customers_to_geo", "from": "customers", "to": "geolocation", "from_columns": ["zip"], "to_columns": ["zip"]})
    assert _code(SemanticModel(doc), ["revenue"], ["geolocation.state"]) == "invalid_relationship"

    # 상품 행이 없는 주문(o5)이 있어도, 데이터셋 순서와 상관없이 주문 기준으로 센다
    doc = _doc()
    doc["metrics"].append({"name": "buyers", "expression": {"sql": "COUNT(DISTINCT customers.customer_unique_id)"}, "custom_extensions": APPROVED})
    shipped = ["orders.order_status = 'shipped'"]
    assert rows(con, SemanticModel(doc), ["buyers"], where=shipped) == [(2,)]
    doc["datasets"].sort(key=lambda d: d["name"] != "order_items")  # order_items 를 맨 앞으로
    assert rows(con, SemanticModel(doc), ["buyers"], where=shipped) == [(2,)]

    # 달력 일수: o6 은 자정을 넘겨 1일, o7 은 3일. 기존 o1 5일, o2 10일 → 평균 19/4
    m = SemanticModel(_doc())
    assert rows(con, m, ["avg_delivery_days"], allow_draft=True) == [(4.75,)]
    pg = " ".join(m.compile(["avg_delivery_days"], allow_draft=True, dialect="postgres").split())
    assert "CAST(orders.order_delivered_customer_date AS DATE) - CAST(orders.order_purchase_timestamp AS DATE)" in pg


def test_query_timeout(tmp_path):
    """실행 시간 제한이 없어 무한 재귀 CTE 가 끝나지 않던 경우."""
    import time

    from sqlalchemy.exc import OperationalError

    from t2s.db import Database

    make_db(str(tmp_path / "demo.sqlite")).close()
    db = Database(url=f"sqlite:///{tmp_path / 'demo.sqlite'}", timeout_s=0.3)
    started = time.monotonic()
    with pytest.raises(OperationalError):
        db.execute("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT MAX(n) FROM r")
    assert time.monotonic() - started < 5
    assert db.execute("SELECT COUNT(*) FROM orders")["rows"] == [[4]]  # 제한이 다음 실행에 남지 않는다


def test_refusals(model):
    with pytest.raises(EngineError) as e:  # 평균은 주문 행 단위라 주문상품을 거쳐 카테고리로 묶으면 부푼다
        model.compile(["avg_delivery_days"], ["products.product_category_name"], allow_draft=True)
    assert (e.value.code, e.value.candidates) == ("fan_out_risk", ["order_items"])

    with pytest.raises(EngineError) as e:
        model.compile(["avg_delivery_days"])
    assert e.value.code == "metric_not_approved"

    doc = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    doc["relationships"].append(
        {"name": "order_items_to_customers", "from": "order_items", "to": "customers",
         "from_columns": ["order_id"], "to_columns": ["customer_id"], "custom_extensions": APPROVED}
    )
    with pytest.raises(EngineError) as e:  # 주문상품 → 고객 길이 직접·주문 경유 두 개
        SemanticModel(doc).compile(["revenue"], ["customers.customer_state"])
    assert e.value.code == "ambiguous_join_path" and len(e.value.candidates) == 2


def test_draft_relationship_not_used(model):
    """approved 가 아닌 연결은 조인 경로로 쓰지 않는다. Olist 의 order_items_to_sellers 는 draft 다."""
    with pytest.raises(EngineError) as e:
        model.compile(["revenue"], ["sellers.seller_state"])
    assert e.value.code == "relationship_not_approved" and "order_items_to_sellers" in str(e.value)
    assert "sellers" not in model.catalog().split("연결")[1]  # LLM 에게 주는 요약에도 draft 연결은 없다
    doc = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    for r in doc["relationships"]:
        if r["name"] == "order_items_to_sellers":
            r["custom_extensions"] = APPROVED
    assert "JOIN sellers" in SemanticModel(doc).compile(["revenue"], ["sellers.seller_state"])



def test_unreachable_names_the_blocking_relationship():
    """길이 없을 때는 그 길 위에서 막힌 연결만 이유로 댄다. 대상 테이블에 닿기만 하는 draft 연결을 탓하지 않는다."""
    doc = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    doc["metrics"] = [m for m in doc.get("metrics", []) if m["name"] != "review_probe"]
    doc["metrics"].append({"name": "review_probe", "expression": {"sql": "AVG(order_reviews.review_score)"}, "custom_extensions": APPROVED})
    for r in doc["relationships"]:  # 샘플 정의의 승인 상태와 상관없이 리뷰 → 주문을 승인 전으로 둔다
        if r["name"] == "order_reviews_to_orders":
            r["custom_extensions"] = [{"vendor_name": "TEXT2SQL", "data": '{"status": "draft"}'}]
    # 리뷰 → 주문 연결이 draft 라 막힌다. 주문에 닿는 다른 draft 연결(결제)을 대면 안 된다
    with pytest.raises(EngineError) as e:
        SemanticModel(doc).compile(["review_probe"], ["customers.customer_state"])
    assert e.value.code == "relationship_not_approved" and "order_reviews_to_orders" in str(e.value)
    # 리뷰 → 주문을 승인해도 상품 카테고리로 가는 N→1 길은 없다. 상품에 닿는 번역 연결(draft)을 대면 안 된다
    for r in doc["relationships"]:
        if r["name"] == "order_reviews_to_orders":
            r["custom_extensions"] = APPROVED
    assert "JOIN customers" in SemanticModel(doc).compile(["review_probe"], ["customers.customer_state"])
    with pytest.raises(EngineError) as e:
        SemanticModel(doc).compile(["review_probe"], ["products.product_category_name"])
    assert e.value.code == "no_join_path"


def test_preview_uses_draft_joins_only_when_asked():
    """미리 보기(draft_joins=True)는 그 호출에서만 승인 전 연결을 쓴다. 다음 호출은 다시 승인된 연결만 쓴다."""
    doc = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    for r in doc["relationships"]:  # 샘플 정의의 승인 상태와 상관없이 리뷰 → 주문을 승인 전으로 둔다
        if r["name"] == "order_reviews_to_orders":
            r["custom_extensions"] = [{"vendor_name": "TEXT2SQL", "data": '{"status": "draft"}'}]
    model = SemanticModel(doc)
    with pytest.raises(EngineError) as e:
        model.compile(["avg_review_score"], ["customers.customer_state"], allow_draft=True)
    assert e.value.code == "relationship_not_approved" and "order_reviews_to_orders" in str(e.value)
    assert "JOIN orders" in model.compile(["avg_review_score"], ["customers.customer_state"], allow_draft=True, draft_joins=True)
    with pytest.raises(EngineError):
        model.compile(["avg_review_score"], ["customers.customer_state"], allow_draft=True)

def test_derive_whitelist(con, model):
    """지표 조합 식은 결과 컬럼·숫자·사칙연산만. 하위 쿼리로 정의 밖 집계를 실행하던 구멍."""
    sql = model.compile(["revenue", "orders.order_count"], dialect="sqlite")
    assert con.execute(derive(sql, "revenue / order_count * 1.0", [], ["revenue", "orders.order_count"])).fetchone() == (200 / 3,)
    for bad in ("(SELECT SUM(price) FROM order_items)", "revenue + nope", "orders.order_count", "revenue || 'x'", "ABS(revenue)", "revenue /"):
        with pytest.raises(EngineError) as e:
            derive(sql, bad, [], ["revenue", "orders.order_count"])
        assert e.value.code == "invalid_expr", bad
