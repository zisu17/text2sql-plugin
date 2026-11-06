"""정의 검사(t2s check 의 findings): 정적 규칙과 실제 DB 대조. LLM 없이 임시 SQLite 로 본다."""
import copy

import pytest
import yaml

from tests.conftest import CUSTOMER_COUNT, MODEL, make_db, set_ext
from t2s.db import Database
from t2s.definition import findings

BASE = yaml.safe_load(MODEL.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    make_db(str(tmp_path / "demo.sqlite")).close()
    return Database(url=f"sqlite:///{tmp_path / 'demo.sqlite'}")


@pytest.fixture
def doc():
    return copy.deepcopy(BASE)


def _dataset(doc, name):
    return next(d for d in doc["datasets"] if d["name"] == name)


def _metric(doc, name):
    return next(m for m in doc["metrics"] if m["name"] == name)


def test_clean_definition(doc, db):
    r = findings(doc, db)
    assert r["ok"] and not r["problems"] and "revenue (approved): 200.0" in r["samples"]
    assert findings(doc) == {"ok": True, "problems": [], "notes": [], "samples": [], "pending": []}  # DB 없으면 정적 검사만


def test_draft_metric_and_bad_link(doc, db):
    m = yaml.safe_load(CUSTOMER_COUNT)
    set_ext(m, status="draft")
    _dataset(doc, "customers").setdefault("metrics", []).append(m)
    # to 쪽 order_items.order_id 는 주문 하나에 여러 행이라 한 행 쪽이 아니다
    doc["relationships"].append({"name": "bad_link", "from": "customers", "to": "order_items",
                                 "from_columns": ["customer_id"], "to_columns": ["order_id"]})
    r = findings(doc, db)
    assert "customers.customer_count (draft): 2" in r["samples"]
    assert any("기본키가 아니라 N:1 연결로 쓸 수 없음" in p for p in r["problems"])


@pytest.mark.parametrize("change, expected", [
    (lambda d: _dataset(d, "customers")["fields"].remove(  # orders_to_customers 가 가리키는 필드 삭제
        next(f for f in _dataset(d, "customers")["fields"] if f["name"] == "customer_id")), "customers 에 없는 필드 customer_id"),
    (lambda d: _dataset(d, "orders")["fields"].append({"name": "order_count", "expression": {"sql": "order_id"}}), "필드·지표 이름 중복 order_count"),
    (lambda d: _dataset(d, "orders")["metrics"].append({"name": "scoped", "expression": {"sql": "COUNT(orders.order_id)"}}), "접두어 없이 쓴다"),
    (lambda d: next(f for f in _dataset(d, "customers")["fields"] if f["name"] == "customer_state").update(datatype="Money"), "Ossie 자료형이 아님"),
    (lambda d: d["relationships"].append({"name": "items_to_customers", "from": "order_items", "to": "customers",
                                          "from_columns": ["order_id"], "to_columns": ["customer_id"]}), "모호함"),
    (lambda d: set_ext(_metric(d, "revenue"), decision="nope"), "decisions 에 없는 결정 nope"),
], ids=["missing_field", "duplicate_name", "prefixed_metric", "bad_datatype", "ambiguous_path", "unknown_decision"])
def test_static_rules(doc, change, expected):
    change(doc)
    r = findings(doc)
    assert not r["ok"] and any(expected in p for p in r["problems"]), r["problems"]


def test_expected_values(doc, db):
    """담당자 예상값이 어긋나면 문제가 아니라 확인 요청(notes)이다. where 를 주면 그 범위로 견준다."""
    set_ext(_metric(doc, "revenue"), expected={"value": 150})
    r = findings(doc, db)
    assert r["ok"] and r["notes"] == ["revenue: 담당자 예상 150, 계산 200. 정의나 예상값을 다시 확인한다"]
    jan = ["orders.order_purchase_timestamp >= '2018-01-01'", "orders.order_purchase_timestamp < '2018-02-01'"]
    set_ext(_metric(doc, "revenue"), expected={"value": 180, "where": jan})
    assert findings(doc, db)["notes"] == []


def test_broken_extension_is_reported(doc, db):
    """확장 칸 JSON 이 깨져도 예외 없이 문제로 보고한다."""
    _metric(doc, "revenue")["custom_extensions"][0]["data"] = "{status: approved}"
    r = findings(doc, db)
    assert not r["ok"] and r["problems"]
