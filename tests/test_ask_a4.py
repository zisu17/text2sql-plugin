"""A4 selects results for review without seeing gold data or earlier candidates."""

import json

import pytest

from scripts import ask_a4


def rows(*values, name="value", truncated=False):
    return {"ok": True, "columns": [name], "rows": [[v] for v in values], "truncated": truncated}


FAILED = {"error": {"kind": "syntax"}}


@pytest.mark.parametrize(("outputs", "status", "matching"), [
    ([rows(1), {"result": rows(1.0, name="count")}], "review_agreement", [1, 2]),
    ([rows(), rows()], "review_agreement", [1, 2]),
    ([FAILED, rows(0), rows(0)], "review_agreement", [2, 3]),
    ([rows(True), rows(1), rows("1")], "resolve_interpretation", []),
    ([rows(1, 2), rows(2, 1), rows(1, 2, 2)], "resolve_interpretation", []),
    ([rows(None), rows(""), rows(0)], "resolve_interpretation", []),
    ([rows(1), rows(1, truncated=True), FAILED], "resolve_interpretation", []),
    ([FAILED, FAILED, rows(1, truncated=True)], "check_execution", []),
    ([{"error": {"kind": "graph_unavailable"}}], "blocked", []),
    ([{"error": {"kind": "policy"}}], "blocked", []),
])
def test_decisions(monkeypatch, outputs, status, matching):
    prompts = []
    executed = []
    def generate(prompt):
        prompts.append(prompt)
        return f"SELECT {len(prompts)}"
    def execute(route, sql):
        assert route == "sql"
        executed.append(sql)
        return outputs[len(executed) - 1]
    monkeypatch.setattr(ask_a4, "generate", generate)
    monkeypatch.setattr(ask_a4, "execute", execute)
    decision = ask_a4.compare_candidates("sql", "How many?", "table and fields")
    assert len(prompts) == len(outputs) and len(set(prompts)) == 1
    assert decision["status"] == status and decision.get("matching", []) == matching
    assert [c["sql"] for c in decision["candidates"]] == executed
    if matching:
        assert decision["execution"] == outputs[matching[0] - 1]
    else:
        assert "review_candidate" not in decision and "execution" not in decision


def test_wrapper_uses_stdin_and_separate_workdirs(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    from subprocess import CompletedProcess
    monkeypatch.setenv("GRAPH_MODEL", "demo")
    monkeypatch.setenv("GRAPH_VERSION_ID", "v1")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    monkeypatch.setenv("TEXT2SQL_MCP_TOKEN", "secret")
    workdirs = []
    def run(args, **kwargs):
        if args[0] == "codex":
            assert kwargs["input"].count("count widgets") == 1
            assert "TEXT2SQL_MCP_TOKEN" not in kwargs["env"] and "DATABASE_URL" not in kwargs["env"]
            assert "gpt-6-luna" in args and "--ephemeral" in args
            workdirs.append(kwargs["cwd"])
            Path(args[args.index("-o") + 1]).write_text('{"sql":"SELECT COUNT(*) FROM widgets"}')
            return CompletedProcess(args, 0)
        assert args == ["sh", str(ask_a4.WRAPPER), "run", "-"]
        assert kwargs["input"] == "SELECT COUNT(*) FROM widgets"
        return CompletedProcess(args, 0, json.dumps(rows(4)), "")
    import io
    monkeypatch.setattr(ask_a4.subprocess, "run", run)
    monkeypatch.setattr(ask_a4.sys, "argv", ["ask_a4.py", "run"])
    monkeypatch.setattr(ask_a4.sys, "stdin", io.StringIO('{"question":"count widgets","context":"widgets(id)"}'))
    assert ask_a4.main() == 0
    assert len(set(workdirs)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "review_agreement"
