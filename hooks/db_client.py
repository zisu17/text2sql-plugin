"""PreToolUse(Bash): DB 클라이언트를 직접 부르면 사용자 확인을 받는다.

모든 Bash 명령에서 불리고, sqlite3·psql·mysql·duckdb 를 부를 때만 묻는다. Codex 는 ask 를 지원하지 않아 그대로 진행된다.
막지(deny) 않고 묻기(ask)만 한다. 실제 보장은 읽기 전용 계정과 t2s 가드다.
"""
import json
import re
import sys

CLIENT = re.compile(r"(?:^|[\s;&|(])(sqlite3|psql|mysql|duckdb)(?=\s|$)")

try:
    command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
except (ValueError, AttributeError):
    sys.exit(0)

match = CLIENT.search(command) if isinstance(command, str) else None
if match:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": f"{match.group(1)} 직접 접속은 t2s 가드를 지나지 않는다. 데이터 조회는 t2s run 이나 t2s compile --run 으로 한다.",
    }}, ensure_ascii=False))
