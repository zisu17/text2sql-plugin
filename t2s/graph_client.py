"""Load the active semantic definition from the Graph server without a local file.

The server exposes the full model at GET /snapshot, outside MCP, so that no LLM can pull it into
its context. Only this process reads it, to compile and validate SQL in memory.

Each version is kept on disk ($XDG_CACHE_HOME or ~/.cache, t2s/snapshots/<version_id>.json). The version id is
made from the definition file's bytes, so one id always means the same definition. When GRAPH_VERSION_ID names a
version already kept there, the server is not called: the answer uses exactly the version the question started with.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx2


DEFAULT_URL = "https://text2sql-graph-mcp-production.up.railway.app/mcp"


class GraphClientError(RuntimeError):
    pass


def _snapshot_url(model: str | None) -> str:
    """TEXT2SQL_MCP_URL 은 MCP 주소(.../mcp)다. 같은 서버의 /snapshot[/모델] 로 바꾼다."""
    base = os.getenv("TEXT2SQL_MCP_URL", DEFAULT_URL).rstrip("/").removesuffix("/mcp")
    return f"{base}/snapshot" + (f"/{quote(model, safe='')}" if model else "")


def _server_code(response: httpx2.Response) -> str:
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" {code}" if isinstance(code, str) and code.isidentifier() else ""


def fetch_model(model: str | None = None, *, transport: httpx2.BaseTransport | None = None) -> dict:
    """{"version_id", "definition", ...}. 실패는 원인별 안내를 담은 GraphClientError. 토큰 값은 메시지에 넣지 않는다."""
    token = os.getenv("TEXT2SQL_MCP_TOKEN")
    if not token:
        raise GraphClientError("TEXT2SQL_MCP_TOKEN 이 없다. Graph MCP 인증을 설정한다")
    cached = _cached(os.getenv("GRAPH_VERSION_ID"), model)
    if cached:
        return cached
    try:
        with httpx2.Client(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0),
                           transport=transport) as http:
            response = http.get(_snapshot_url(model))
    except httpx2.TimeoutException as error:
        raise GraphClientError("Graph 서버가 30초 안에 응답하지 않았다. 잠시 뒤 다시 시도한다") from error
    except httpx2.TransportError as error:
        raise GraphClientError(f"Graph 서버에 연결하지 못했다({type(error).__name__}). TEXT2SQL_MCP_URL 주소와 네트워크를 확인한다") from error
    except Exception as error:  # 잘못된 주소·헤더 값 등. 원문에 토큰이 섞일 수 있어 종류만 적는다
        raise GraphClientError(f"Graph 서버 호출 실패: {type(error).__name__}") from error
    status = response.status_code
    if status in (401, 403):
        raise GraphClientError(f"Graph 서버가 인증을 거절했다({status}). TEXT2SQL_MCP_TOKEN 값을 확인한다")
    if status == 404:
        raise GraphClientError("활성 모델을 찾지 못했다(404). GRAPH_MODEL 이름을 확인한다. "
                               "Graph 서버가 0.3.0 보다 낮으면 /snapshot 경로가 없어 서버를 먼저 올려야 한다")
    if status >= 500:
        raise GraphClientError(f"Graph 서버 쪽 오류다({status}{_server_code(response)}). 서버 상태와 Neo4j 연결을 확인한다")
    if status != 200:
        raise GraphClientError(f"Graph 서버가 예상하지 못한 응답을 했다({status}). TEXT2SQL_MCP_URL 주소를 확인한다")
    try:
        snapshot = response.json()
    except ValueError as error:
        raise GraphClientError("Graph 서버 응답이 JSON 이 아니다. TEXT2SQL_MCP_URL 주소를 확인한다") from error
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("definition"), dict):
        raise GraphClientError("Graph 서버 응답에 실행용 정의가 없다")
    if not snapshot.get("model") or not snapshot.get("version_id"):
        raise GraphClientError("Graph 서버 응답에 모델 또는 활성 버전이 없다")
    if model and snapshot["model"] != model:
        raise GraphClientError("Graph 서버 응답 모델이 요청과 다르다")
    _store(snapshot)
    return snapshot


_VERSION_ID = re.compile(r"[0-9A-Za-z_-]{1,128}")  # 파일 이름으로 쓰므로 경로 문자를 막는다


def _cache_path(version_id: object) -> Path | None:
    if not isinstance(version_id, str) or not _VERSION_ID.fullmatch(version_id):
        return None
    base = os.getenv("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "t2s" / "snapshots" / f"{version_id}.json"


def _cached(version_id: str | None, model: str | None) -> dict | None:
    path = _cache_path(version_id)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None
    ok = (isinstance(snapshot, dict) and snapshot.get("version_id") == version_id
          and isinstance(snapshot.get("definition"), dict) and (not model or snapshot.get("model") == model))
    return snapshot if ok else None


def _store(snapshot: dict) -> None:
    """버전별 사본을 남긴다. 속도용이라 못 써도 조회는 그대로 된다."""
    path = _cache_path(snapshot["version_id"])
    if path is None:
        return
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # 본인만 읽는 권한(0600)으로 만든다
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
