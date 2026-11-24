"""훅이 Claude Code(Bash)·Codex(apply_patch) 입력을 받아 맞게 반응하는지. 훅은 plain python3 로 돈다."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from tests.conftest import MODEL

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
ROOT = HOOKS.parent


def hook(script: Path, payload: dict) -> dict | None:
    out = subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and not out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def test_db_client():
    asked = hook(HOOKS / "db_client.py", {"tool_name": "Bash", "tool_input": {"command": "ls && psql -c 'select 1'"}})
    assert asked["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert hook(HOOKS / "db_client.py", {"tool_name": "Bash", "tool_input": {"command": "ls psql_notes"}}) is None
    assert hook(HOOKS / "db_client.py", {"tool_input": None}) is None


def _decision(command: str) -> str | None:
    out = hook(HOOKS / "db_client.py", {"tool_name": "Bash", "tool_input": {"command": command}})
    return out["hookSpecificOutput"]["permissionDecision"] if out else None


def test_db_client_paths_and_more_clients():
    for command in ("/usr/bin/psql -c 'select 1'", "/opt/homebrew/bin/sqlite3 x.db", "echo `psql -At -c 'select 1'`",
                    "x=$(mysql -e 'select 1')", "pgcli postgres://localhost/db", "mycli -h localhost",
                    "clickhouse-client --query 'select 1'", "cd /tmp; duckdb x.duckdb",
                    "psql<query.sql", "sqlite3>out.txt x.db", "\"psql\" -c 'select 1'", "'mysql' -e 'select 1'", "\\psql -c 'select 1'"):
        assert _decision(command) == "ask", command  # 묻기만 하고 막지(deny) 않는다
    for command in ("psql_notes.md", "pip install mysqlclient", "grep sqlite3_open src.c", "echo clickhouse-client-docs"):
        assert _decision(command) is None, command
    # 알려진 오탐: 경로가 클라이언트 이름으로 끝나면 묻는다. 막지 않으므로 받아들인다
    assert _decision("ls /var/lib/mysql") == "ask"


def test_check_definition_codex_patch(tmp_path):
    # 플러그인 모양을 흉내 낸다: hooks/ 옆 scripts/t2s 를 지금 파이썬의 t2s 로 둔다.
    (tmp_path / "hooks").mkdir()
    shutil.copy(HOOKS / "check_definition.py", tmp_path / "hooks")
    (tmp_path / "scripts").mkdir()
    stub = tmp_path / "scripts" / "t2s"
    stub.write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -m t2s.cli "$@"\n')
    stub.chmod(0o755)
    doc = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    doc["relationships"][0]["from_columns"] = ["nope"]
    (tmp_path / "bad.ossie.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: bad.ossie.yaml\n@@\n-a\n+b\n*** End Patch\n"
    out = hook(tmp_path / "hooks" / "check_definition.py", {"tool_name": "apply_patch", "tool_input": {"command": patch}, "cwd": str(tmp_path)})
    assert out["decision"] == "block" and "없는 필드 nope" in out["reason"]
    assert hook(tmp_path / "hooks" / "check_definition.py", {"tool_input": {"file_path": "x.py"}}) is None
