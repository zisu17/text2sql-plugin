"""지표 질문의 기본 경로(LLM 이 쓴 SQL + AGG(별칭.지표), t2s sql)가 골든셋 정답을 내는지 본다.

실제 Olist 정의처럼 매출·배송비·리뷰 평균을 데이터셋 지표(order_lines·review_lines)로 둔 사본에서 계산한다.
테스트 정의의 모델 지표(revenue 등)는 2층 측정이 아니라 AGG 로 쓸 수 없다. 그런 지표만 t2s compile 로 계산한다.
"""
import copy

import pytest
import yaml

from tests.conftest import GOLDEN, MODEL
from t2s.engine import SemanticModel
from t2s.measures import rewrite

APPROVED = [{"vendor_name": "TEXT2SQL", "data": {"status": "approved"}}]
NOT_CANCELED = "order_status NOT IN ('canceled', 'unavailable')"
ITEMS = "JOIN order_items AS i ON i.order_id = l.order_id AND i.order_item_id = l.order_item_id"

# 골든셋 지표 문항 id → LLM 이 t2s sql 로 쓸 SQL. 정답은 골든셋의 answer 와 같아야 한다
AGG = {
    1: f"SELECT AGG(l.revenue) AS revenue FROM order_lines AS l {ITEMS} JOIN orders AS o ON o.order_id = i.order_id "
       "WHERE o.order_purchase_timestamp >= '2018-01-01' AND o.order_purchase_timestamp < '2018-02-01'",
    2: "SELECT AGG(l.revenue) AS revenue FROM order_lines AS l",
    3: "SELECT AGG(o.order_count) AS n FROM orders AS o "
       "WHERE o.order_purchase_timestamp >= '2018-02-01' AND o.order_purchase_timestamp < '2018-03-01'",
    4: f"SELECT strftime('%Y-%m', o.order_purchase_timestamp) AS m, AGG(l.revenue) AS revenue FROM order_lines AS l {ITEMS} "
       "JOIN orders AS o ON o.order_id = i.order_id GROUP BY 1 ORDER BY 1",
    6: f"SELECT p.product_category_name, AGG(l.revenue) AS revenue FROM order_lines AS l {ITEMS} "
       "JOIN products AS p ON p.product_id = i.product_id GROUP BY 1 ORDER BY revenue DESC LIMIT 1",
    7: "SELECT AGG(l.revenue) AS revenue FROM order_lines AS l WHERE l.customer_state = 'SP'",
    8: "SELECT c.customer_state, AGG(l.revenue) AS revenue, AGG(o.order_count) AS n FROM orders AS o "
       "JOIN customers AS c ON c.customer_id = o.customer_id LEFT JOIN order_items AS i ON i.order_id = o.order_id "
       "LEFT JOIN order_lines AS l ON l.order_id = i.order_id AND l.order_item_id = i.order_item_id GROUP BY 1 ORDER BY 1",
    9: "SELECT ROUND(AGG(l.revenue) / AGG(o.order_count), 2) AS per_order FROM orders AS o "
       "LEFT JOIN order_items AS i ON i.order_id = o.order_id "
       "LEFT JOIN order_lines AS l ON l.order_id = i.order_id AND l.order_item_id = i.order_item_id",
    10: f"SELECT AGG(l.revenue) AS revenue FROM order_lines AS l {ITEMS} JOIN orders AS o ON o.order_id = i.order_id "
        "WHERE o.order_purchase_timestamp >= '2017-01-01' AND o.order_purchase_timestamp < '2018-01-01'",
    12: "SELECT AGG(r.avg_review_score) AS score FROM review_lines AS r",
    13: "SELECT p.product_category_name, AGG(o.order_count) AS n FROM orders AS o "
        "JOIN order_items AS i ON i.order_id = o.order_id JOIN products AS p ON p.product_id = i.product_id GROUP BY 1 ORDER BY 1",
    16: "SELECT AGG(l.revenue) + AGG(l.freight) AS with_freight FROM order_lines AS l",
}
# 정답 표기만 다른 문항: 묶음 없는 집계는 행이 없어도 한 행(NULL)을 낸다. 엔진은 묶음이 비면 행을 내지 않는다
SAME_MEANING = {10: [[None]]}


def _olist_shaped() -> SemanticModel:
    """테스트 정의의 모델 지표를 실제 Olist 정의처럼 조회문 데이터셋의 지표로 옮긴 사본."""
    doc = copy.deepcopy(yaml.safe_load(MODEL.read_text(encoding="utf-8")))
    doc["metrics"] = []
    fields = lambda names: [{"name": n, "expression": {"sql": n}, "datatype": "String"} for n in names]
    doc["datasets"] += [
        {"name": "order_lines", "primary_key": ["order_id", "order_item_id"],
         "source": ("SELECT oi.order_id, oi.order_item_id, oi.price, oi.freight_value, o.order_status, c.customer_state "
                    "FROM order_items AS oi LEFT JOIN orders AS o ON o.order_id = oi.order_id "
                    "LEFT JOIN customers AS c ON c.customer_id = o.customer_id"),
         "fields": fields(["order_id", "order_item_id", "price", "freight_value", "order_status", "customer_state"]),
         "metrics": [
             {"name": "revenue", "expression": {"sql": f"SUM(price) FILTER (WHERE {NOT_CANCELED})"}, "nonportable_extensions": APPROVED},
             {"name": "freight", "expression": {"sql": f"SUM(freight_value) FILTER (WHERE {NOT_CANCELED})"}, "nonportable_extensions": APPROVED},
         ]},
        {"name": "review_lines", "primary_key": ["review_id", "order_id"],
         "source": ("SELECT r.review_id, r.order_id, r.review_score, o.order_status "
                    "FROM order_reviews AS r LEFT JOIN orders AS o ON o.order_id = r.order_id"),
         "fields": fields(["review_id", "order_id", "review_score", "order_status"]),
         "metrics": [{"name": "avg_review_score", "expression": {"sql": "AVG(review_score) FILTER (WHERE order_status = 'delivered')"},
                      "nonportable_extensions": APPROVED}]},
    ]
    doc["relationships"] += [
        {"name": "order_lines_to_order_items", "from": "order_lines", "to": "order_items",
         "from_columns": ["order_id", "order_item_id"], "to_columns": ["order_id", "order_item_id"], "nonportable_extensions": APPROVED},
    ]
    return SemanticModel(doc)


def _same(got, want) -> bool:
    cell = lambda g, w: (round(g, 2) == round(w, 2)) if isinstance(w, float) and g is not None else (str(g).startswith(str(w)) if w is not None else g is None)
    return len(got) == len(want) and all(len(g) == len(w) and all(map(cell, g, w)) for g, w in zip(got, want))


def test_every_metric_question_has_an_agg_query():
    metric_items = {g["id"] for g in yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) if g.get("call")}
    assert metric_items - {5} == set(AGG)  # 5번은 4번과 같은 계산(월별 매출 비교)이다


@pytest.mark.parametrize("item", [g for g in yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) if g["id"] in AGG],
                         ids=lambda g: str(g["id"]))
def test_agg_query_matches_golden_answer(con, item):
    out = rewrite(AGG[item["id"]], _olist_shaped(), read="sqlite", dialect="sqlite")
    assert out["undefined"] == [] and out["measures"]  # 정의된 지표만으로 계산했다
    got = con.execute(out["sql"]).fetchall()
    assert _same(got, SAME_MEANING.get(item["id"], item["answer"])), (item["id"], got)
