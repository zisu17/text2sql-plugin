"""테스트 공통 재료. Olist 모양의 작은 SQLite 데이터와 정의 파일 경로. LLM 은 쓰지 않는다."""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from t2s.engine import SemanticModel

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
MODEL = FIXTURE_DIR / "olist" / "sales.ossie.yaml"  # 시험용 정의. 테스트 정답이 이 파일에 맞춰져 있다
GOLDEN = FIXTURE_DIR / "golden_olist.yaml"


@pytest.fixture(autouse=True)
def _graph_env(monkeypatch):
    monkeypatch.delenv("GRAPH_VERSION_ID", raising=False)


def graph_server(doc: dict, version: str = "published-v1", calls: list | None = None):
    """Graph 서버의 POST /definition 흉내. 요청한 이름이 닿는 데이터셋과 그 사이 연결 경로만 돌려준다
    (text2sql-mcp 의 scope_datasets 와 같은 규칙). calls 에 요청과 돌려준 데이터셋 이름을 남긴다."""
    def distances(start, step):
        dist, frontier = {start: 0}, {start}
        for hop in range(1, 5):
            frontier = {n for f in frontier for n in step.get(f, ())} - dist.keys()
            dist.update(dict.fromkeys(frontier, hop))
        return dist

    def fetch(model=None, *, datasets=(), tables=(), metrics=(), with_definition=True):
        names = {d["name"].lower(): d["name"] for d in doc["datasets"]}
        table_of = {d["name"]: d["source"].strip().lower().rpartition(".")[2] for d in doc["datasets"]
                    if " " not in d["source"].strip()}
        wanted_tables = {t.lower().rpartition(".")[2] for t in tables}
        wanted = {names[n.lower()] for n in datasets if n.lower() in names}
        wanted |= {d for d, t in table_of.items() if t in wanted_tables}
        top = []
        for m in metrics:
            ds, dot, short = m.rpartition(".")
            wanted |= {d["name"] for d in doc["datasets"] for x in d.get("metrics", [])
                       if x["name"] == (short if dot else m) and (not dot or d["name"] == ds)}
            for x in doc.get("metrics", []) if not dot else []:
                if x["name"] == m:  # 모델 지표는 식에 쓴 데이터셋을 데려온다
                    top.append(m)
                    wanted |= {names[a.lower()] for a in re.findall(r"\b(\w+)\.\w+", x["expression"]["sql"]) if a.lower() in names}
        reads = {w.lower() for w in wanted}  # 범위 데이터셋을 읽는 모델 지표는 정의만 함께 준다
        top += [x["name"] for x in doc.get("metrics", []) if x["name"] not in top
                and {a.lower() for a in re.findall(r"\b(\w+)\.\w+", x["expression"]["sql"])} & reads]
        out_edges, into = {}, {}
        for r in doc.get("relationships", []):
            out_edges.setdefault(r["from"], set()).add(r["to"])
            into.setdefault(r["to"], set()).add(r["from"])
        keep = set(wanted)
        if wanted:
            back = {w: distances(w, into) for w in wanted}
            reach_all = set.intersection(*(set(b) for b in back.values()))
            for b in (wanted if reach_all & wanted else wanted | reach_all):
                ahead = distances(b, out_edges)
                for dist in back.values():
                    keep |= {n for n, d in ahead.items() if n in dist and d + dist[n] <= 4}
        read = {t.lower().rpartition(".")[2] for d in doc["datasets"] if d["name"] in keep and d["name"] not in table_of
                for t in re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", d["source"], re.I)}
        keep |= {d for d, t in table_of.items() if t in read}  # 계산용 데이터셋이 읽는 표의 데이터셋
        meta = {"tenant": "olist", "model": doc["name"], "version_id": version, "name": doc["name"],
                "revision": SemanticModel(doc).revision,
                "defined_tables": sorted(t for t in table_of.values() if t in wanted_tables),
                "datasets_total": len(doc["datasets"])}
        if calls is not None:
            calls.append({"datasets": sorted(datasets), "tables": sorted(tables), "metrics": sorted(metrics),
                          "returned": sorted(keep) if with_definition else None})
        if not with_definition:
            return meta
        part = {**doc, "datasets": [d for d in doc["datasets"] if d["name"] in keep],
                "metrics": [m for m in doc.get("metrics", []) if m["name"] in top],
                "relationships": [r for r in doc.get("relationships", []) if r["from"] in keep and r["to"] in keep]}
        return {**meta, "definition": part}
    return fetch

# o3 는 취소 주문이라 매출·주문 수에서 빠진다. c1 과 c3 은 같은 사람(u1)이다
FIXTURE_SQL = """
CREATE TABLE customers (customer_id TEXT PRIMARY KEY, customer_unique_id TEXT, customer_state TEXT);
INSERT INTO customers VALUES ('c1', 'u1', 'SP'), ('c2', 'u2', 'RJ'), ('c3', 'u1', 'SP');
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_category_name TEXT);
INSERT INTO products VALUES ('p1', 'beleza'), ('p2', 'esporte');
CREATE TABLE orders (
  order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT,
  order_purchase_timestamp TIMESTAMP, order_delivered_customer_date TIMESTAMP
);
INSERT INTO orders VALUES
  ('o1', 'c1', 'delivered', '2018-01-05 10:00:00', '2018-01-10 10:00:00'),
  ('o2', 'c2', 'delivered', '2018-01-20 10:00:00', '2018-01-30 10:00:00'),
  ('o3', 'c3', 'canceled',  '2018-02-03 10:00:00', NULL),
  ('o4', 'c1', 'shipped',   '2018-02-10 10:00:00', NULL);
CREATE TABLE order_items (
  order_id TEXT, order_item_id INTEGER, product_id TEXT, price REAL, freight_value REAL,
  PRIMARY KEY (order_id, order_item_id)
);
INSERT INTO order_items VALUES
  ('o1', 1, 'p1', 100.0, 10.0), ('o1', 2, 'p2', 50.0, 5.0), ('o2', 1, 'p1', 30.0, 3.0),
  ('o3', 1, 'p2', 999.0, 9.0), ('o4', 1, 'p2', 20.0, 2.0);
-- 정의에 있는 나머지 공식 스키마 컬럼·테이블. 값은 비워 두고 키만 잇는다
ALTER TABLE orders ADD COLUMN order_approved_at TIMESTAMP;
ALTER TABLE orders ADD COLUMN order_delivered_carrier_date TIMESTAMP;
ALTER TABLE orders ADD COLUMN order_estimated_delivery_date TIMESTAMP;
ALTER TABLE customers ADD COLUMN customer_city TEXT;
ALTER TABLE customers ADD COLUMN customer_zip_code_prefix INTEGER;
ALTER TABLE order_items ADD COLUMN seller_id TEXT DEFAULT 's1';
ALTER TABLE order_items ADD COLUMN shipping_limit_date TIMESTAMP;
ALTER TABLE products ADD COLUMN product_name_lenght REAL;
ALTER TABLE products ADD COLUMN product_description_lenght REAL;
ALTER TABLE products ADD COLUMN product_photos_qty REAL;
ALTER TABLE products ADD COLUMN product_weight_g REAL;
ALTER TABLE products ADD COLUMN product_length_cm REAL;
ALTER TABLE products ADD COLUMN product_height_cm REAL;
ALTER TABLE products ADD COLUMN product_width_cm REAL;
CREATE TABLE sellers (seller_id TEXT PRIMARY KEY, seller_zip_code_prefix INTEGER, seller_city TEXT, seller_state TEXT);
INSERT INTO sellers VALUES ('s1', 1001, 'sao paulo', 'SP');
CREATE TABLE order_payments (
  order_id TEXT, payment_sequential INTEGER, payment_type TEXT, payment_installments INTEGER, payment_value REAL,
  PRIMARY KEY (order_id, payment_sequential)
);
CREATE TABLE order_reviews (
  review_id TEXT, order_id TEXT, review_score INTEGER, review_comment_title TEXT, review_comment_message TEXT,
  review_creation_date TIMESTAMP, review_answer_timestamp TIMESTAMP, PRIMARY KEY (review_id, order_id)
);
CREATE TABLE product_category_name_translation (product_category_name TEXT PRIMARY KEY, product_category_name_english TEXT);
INSERT INTO product_category_name_translation VALUES ('beleza', 'health_beauty'), ('esporte', 'sports_leisure');
"""

CUSTOMER_COUNT = """
name: customer_count
expression: {sql: COUNT(DISTINCT customer_unique_id)}
description: 고객 수
custom_extensions: [{vendor_name: TEXT2SQL, data: '{"status": "approved"}'}]
"""


def set_ext(obj: dict, **values) -> None:
    """정의 항목의 확장 칸(vendor_name TEXT2SQL)에 값을 넣거나 바꾼다."""
    exts = obj.setdefault("custom_extensions", [])
    for x in exts:
        if x["vendor_name"] == "TEXT2SQL":
            x["data"] = json.dumps({**json.loads(x["data"]), **values}, ensure_ascii=False)
            return
    exts.append({"vendor_name": "TEXT2SQL", "data": json.dumps(values, ensure_ascii=False)})


def make_db(path: str = ":memory:") -> sqlite3.Connection:
    """테스트 데이터를 SQLite 에 올린다. 파일 경로를 주면 파일로 남는다."""
    c = sqlite3.connect(path)
    c.executescript(FIXTURE_SQL)
    c.commit()
    return c


@pytest.fixture(scope="module")
def con():
    return make_db()


@pytest.fixture(scope="module")
def model():
    return SemanticModel.load(str(MODEL))
