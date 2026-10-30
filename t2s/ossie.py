"""Ossie 시맨틱 모델 문서와 우리 확장 칸(vendor_name TEXT2SQL)을 다루는 도우미.

엔진·검사·CLI 가 같은 규칙으로 문서를 읽도록 여기에만 둔다. 이 모듈은 다른 t2s 모듈을 import 하지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlglot import exp

READ = "snowflake"  # OSSIE_SQL_2026 식 파서. 기본 파서는 DATEDIFF('day', a, b) 인자를 잘못 읽는다
TIME_TYPES = {"Date", "Time", "DateTime", "DateTimeTz"}


class EngineError(ValueError):
    """LLM이 읽고 요청을 고쳐 다시 시도할 수 있게 코드·후보·힌트를 담는다."""

    def __init__(self, code: str, message: str, candidates: list[str] | None = None, hint: str = ""):
        super().__init__(message)
        self.code, self.candidates, self.hint = code, candidates or [], hint

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "candidates": self.candidates, "hint": self.hint}


def read_yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def expr_sql(obj: dict) -> str:
    """필드·지표의 식 하나. sql > dialects 의 OSSIE_SQL_2026 > ANSI_SQL 순으로 고른다."""
    e = obj["expression"]
    if "sql" in e:
        return e["sql"]
    by_dialect = {d["dialect"]: d["expression"] for d in e["dialects"]}
    for name in ("OSSIE_SQL_2026", "ANSI_SQL"):
        if name in by_dialect:
            return by_dialect[name]
    raise EngineError("unsupported_dialect", f"{obj['name']}: OSSIE_SQL_2026 이나 ANSI_SQL 식이 없음")


def ext(obj: dict) -> dict:
    """우리 확장 칸(vendor_name TEXT2SQL)의 값. 없으면 빈 dict."""
    for x in obj.get("custom_extensions") or []:
        if x["vendor_name"] == "TEXT2SQL":
            return json.loads(x["data"])
    return {}


def status(obj: dict) -> str:
    """승인 상태. 없으면 draft."""
    return ext(obj).get("status", "draft")


def is_time(f: dict) -> bool:
    return f.get("dimension", {}).get("is_time", f.get("datatype") in TIME_TYPES)


def ai(obj: dict) -> dict:
    """ai_context 를 dict 로. 문자열이면 instructions 로 본다."""
    a = obj.get("ai_context") or {}
    return a if isinstance(a, dict) else {"instructions": a}


def qualify(e: exp.Expression, dataset: str) -> exp.Expression:
    """테이블 없이 쓴 컬럼에 dataset 이름을 붙인다."""
    for c in e.find_all(exp.Column):
        if not c.table:
            c.set("table", exp.to_identifier(dataset))
    return e
