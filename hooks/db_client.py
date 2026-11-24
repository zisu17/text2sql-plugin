"""PreToolUse(Bash): DB 클라이언트를 직접 부르면 사용자 확인을 받는다.

모든 Bash 명령에서 불리고, sqlite3·psql·mysql·duckdb·pgcli·mycli·clickhouse-client 를 부를 때만 묻는다.
/usr/bin/psql 같은 절대 경로, `psql …`·$(psql …) 처럼 백틱·괄호 안, "psql"·\\psql 처럼 따옴표·역슬래시로 감싼 것, psql<파일 처럼 리다이렉션을 붙여 쓴 것도 잡는다.
경로가 클라이언트 이름으로 끝나는 명령(ls /var/lib/mysql 등)도 묻게 되지만, 막지 않고 묻기만 하므로 그대로 둔다. Codex 는 ask 를 지원하지 않아 그대로 진행된다.
막지(deny) 않고 묻기(ask)만 한다. 실제 보장은 읽기 전용 계정과 t2s 가드다.
"""
import json
import re
import sys

CLIENT = re.compile(r"(?:^|[\s;&|(`/\"'\\])(sqlite3|psql|mysql|duckdb|pgcli|mycli|clickhouse-client)(?=[\s;&|)`<>\"']|$)")

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
