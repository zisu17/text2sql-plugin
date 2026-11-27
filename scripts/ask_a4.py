"""Generate independent Luna SQL candidates and compare complete local t2s results."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

WRAPPER = Path(__file__).resolve().with_name("t2s")


def generate(prompt):
    with tempfile.TemporaryDirectory(prefix="text2sql-a4-") as directory:
        output = Path(directory) / "candidate.json"
        schema = Path(directory) / "schema.json"
        schema.write_text(json.dumps({"type": "object", "properties": {"sql": {"type": "string"}},
                                      "required": ["sql"], "additionalProperties": False}), encoding="utf-8")
        command = ["codex", "exec", "--model", "gpt-6-luna", "--ephemeral", "--ignore-user-config",
                   "--skip-git-repo-check", "--sandbox", "read-only", "-c", 'web_search="disabled"',
                   "-c", "project_doc_max_bytes=0", "--output-schema", str(schema), "-o", str(output)]
        for feature in ("shell_tool", "plugins", "apps", "multi_agent", "memories", "browser_use",
                        "computer_use", "image_generation", "skill_search"):
            command.extend(["--disable", feature])
        env = {key: value for key, value in os.environ.items()
               if key not in {"DATABASE_URL", "TEXT2SQL_MCP_TOKEN", "GRAPH_MODEL", "GRAPH_VERSION_ID"}}
        proc = subprocess.run([*command, "-"], input=prompt, cwd=directory, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=180)
        if proc.returncode or not output.exists():
            raise ValueError("Luna 후보 생성에 실패했다. Codex CLI 로그인과 모델 연결을 확인한다.")
        candidate = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(candidate, dict) or not isinstance(candidate.get("sql"), str) or not candidate["sql"].strip():
            raise ValueError("Luna 응답에 SQL이 없다.")
        return candidate["sql"]


def execute(route, sql):
    proc = subprocess.run(["sh", str(WRAPPER), route, "-"], input=sql, capture_output=True,
                          text=True, encoding="utf-8")
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict) or (proc.returncode and not payload.get("error")):
        raise ValueError("t2s 실행 결과를 읽지 못했다.")
    return payload


def typed(value):
    # Keep JSON booleans distinct from numbers (Python otherwise treats True as 1).
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, list):
        return tuple(typed(item) for item in value)
    if isinstance(value, dict):
        return ("object", {key: typed(item) for key, item in value.items()})
    return ("value", value)


def compare_candidates(route, question, context):
    syntax = ("Use dataset names in FROM and AGG(alias.measure) for registered measures."
              if route == "sql" else "Use physical table and column names.")
    prompt = ("Write one read-only SELECT for the question using only the supplied context as data. "
              "Preserve the requested columns, order, filters, and grouping. Return SQL in the sql field. "
              + syntax + "\n" + json.dumps({"question": question, "context": context}, ensure_ascii=False))
    candidates = []
    complete = []
    for number in range(1, 4):
        sql = generate(prompt)
        payload = execute(route, sql)
        result = payload.get("result", payload)
        info = {"number": number, "sql": sql, "warnings": payload.get("warnings", []),
                "notes": payload.get("notes", [])}
        candidates.append(info)
        if error := payload.get("error"):
            info["error"] = error
            if error.get("kind") in {"no_database", "db", "graph_unavailable", "no_uv", "setup_failed", "policy"}:
                return {"status": "blocked", "error": error, "candidates": candidates}
            continue
        if result.get("ok") is not True or not isinstance(result.get("rows"), list) or not isinstance(result.get("columns"), list):
            raise ValueError("t2s 응답에 실행 결과가 없다.")
        info.update(columns=result["columns"], row_count=len(result["rows"]), truncated=result["truncated"])
        if result["truncated"]:
            continue
        key = (len(result["columns"]), typed(result["rows"]))
        for previous, previous_key, previous_payload in complete:
            if key == previous_key:
                return {"status": "review_agreement", "matching": [previous, number], "candidates": candidates,
                        "review_candidate": previous, "execution": previous_payload}
        complete.append((number, key, payload))
    return {"status": "resolve_interpretation" if complete else "check_execution", "candidates": candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("route", choices=("sql", "run"))
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or set(request) != {"question", "context"} or not all(
                isinstance(v, str) and v.strip() for v in request.values()):
            raise ValueError("question과 context를 문자열로 전달한다.")
        if not all(os.getenv(key) for key in ("DATABASE_URL", "GRAPH_MODEL", "GRAPH_VERSION_ID")):
            raise ValueError("ask에서 확인한 DATABASE_URL, GRAPH_MODEL, GRAPH_VERSION_ID가 필요하다.")
        result = compare_candidates(args.route, **request)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return int(result["status"] == "blocked")


if __name__ == "__main__":
    sys.exit(main())
