"""DB 접속: 접속 수준 읽기 전용과 컬럼 타입 계열. 읽기 전용은 가드를 거치지 않은 쓰기도 DB 가 거절하는지 본다.

PostgreSQL 은 TEXT2SQL_PG_URL 이 있을 때만 본다: TEXT2SQL_PG_URL=postgresql://... uv run pytest tests/test_db.py
"""
from __future__ import annotations

import os
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from tests.conftest import make_db
from t2s.db import Database, type_family


def test_sqlite_read_only(tmp_path):
    path = str(tmp_path / "demo.sqlite")
    make_db(path).close()
    db = Database(url=f"sqlite:///{path}")
    assert db.execute("SELECT count(*) FROM products")["rows"] == [[2]]
    with pytest.raises(SQLAlchemyError):
        db.execute("INSERT INTO products VALUES ('p9', 'x')")
    assert sqlite3.connect(path).execute("SELECT count(*) FROM products").fetchone() == (2,)
    assert db.readonly_guarantee == "connection"
    # 쓰기 금지 샌드박스에서 정렬·GROUP BY 가 임시 파일 때문에 실패하지 않게 temp_store 가 메모리(2)여야 한다
    assert db.execute("PRAGMA temp_store")["rows"] == [[2]]


def test_sqlite_missing_file(tmp_path):
    """경로 오타로 빈 DB 파일을 새로 만들지 않는다."""
    path = tmp_path / "typo.sqlite"
    with pytest.raises(SQLAlchemyError, match="SQLite 파일이 없다"):
        Database(url=f"sqlite:///{path}").tables
    assert not path.exists()


@pytest.mark.skipif(not os.getenv("TEXT2SQL_PG_URL"), reason="TEXT2SQL_PG_URL 이 없으면 PostgreSQL 은 건너뛴다")
def test_postgres_read_only():
    url, table = os.environ["TEXT2SQL_PG_URL"], "zro_" + uuid.uuid4().hex[:8]
    admin = create_engine(url)
    with admin.begin() as c:
        c.exec_driver_sql(f"CREATE TABLE {table} (id int)")
    try:
        db = Database(url=url)
        assert db.readonly_guarantee == "connection"
        with pytest.raises(SQLAlchemyError):
            db.execute(f"INSERT INTO {table} VALUES (1)")
        db.engine.dispose()
    finally:
        with admin.begin() as c:
            c.exec_driver_sql(f"DROP TABLE {table}")
        admin.dispose()


def test_type_family():
    names = ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP", "DATE", "BIGINT", "NUMERIC(10, 2)", "DOUBLE PRECISION", "VARCHAR(20)", "BYTEA")
    assert [type_family(n) for n in names] == ["DateTimeTz", "DateTime", "Date", "Integer", "Decimal", "Float", "String", "unknown"]
