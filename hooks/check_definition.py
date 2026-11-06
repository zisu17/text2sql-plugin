"""PostToolUse(Edit|Write): *.ossie.yaml 을 고치면 `t2s check --static` 으로 정적 검사한다.

문제가 있으면 decision=block 으로 알린다(이미 쓴 파일은 되돌리지 않는다).
t2s 가 없거나 시간 초과·알 수 없는 출력이면 조용히 통과한다.
Claude Code 는 tool_input.file_path 로, Codex(apply_patch)는 tool_input.command 의 패치 본문으로 고친 파일을 알려 준다.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

PATCHED = re.compile(r"^\*\*\* (?:Update|Add) File: (.+\.ossie\.yaml)\s*$", re.M)
T2S = Path(__file__).resolve().parents[1] / "bin" / "t2s"  # 플러그인에 같이 든 t2s. 훅의 PATH 에는 bin/ 이 없을 수 있다

try:
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input") or {}
    path, patch = tool_input.get("file_path"), tool_input.get("command")
except (ValueError, AttributeError):
    sys.exit(0)
paths = [path] if isinstance(path, str) and path.endswith(".ossie.yaml") else []
if isinstance(patch, str):
    paths += PATCHED.findall(patch)


def problems_of(path: str) -> list:
    try:
        proc = subprocess.run([str(T2S) if T2S.exists() else "t2s", "check", "--static", path], capture_output=True, text=True,
                              timeout=25, cwd=data.get("cwd") or None)
        out = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    if not isinstance(out, dict):
        return []
    error = out.get("error") or {}
    if error.get("kind") == "bad_definition":
        return [error.get("message", "정의 파일을 읽을 수 없음")]
    if out.get("ok") is False and out.get("problems"):
        return [str(p) for p in out["problems"]]
    return []


reasons = []
for path in paths:
    problems = problems_of(path)
    if problems:
        shown = "\n".join(f"- {p}" for p in problems[:10])
        more = f"\n(그 밖에 {len(problems) - 10}건)" if len(problems) > 10 else ""
        reasons.append(f"{path} 정적 검사(t2s check --static)에서 문제 {len(problems)}건:\n{shown}{more}")
if reasons:
    print(json.dumps({"decision": "block", "reason": "\n\n".join(reasons)}, ensure_ascii=False))
