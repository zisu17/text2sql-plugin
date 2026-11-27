"""Load the part of the active semantic definition one t2s command needs, from the Graph server.

t2s never downloads the whole model. A command names the datasets, physical tables and metrics it
touches; the server returns those datasets, the datasets on join paths between them and the joins
among them (POST /definition). The response grows with the question, not with the model. It is not an
MCP tool, so no LLM pulls it into its context; only this process reads it to compile and check SQL.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx2


DEFAULT_URL = "https://text2sql-graph-mcp-production.up.railway.app/mcp"


class GraphClientError(RuntimeError):
    pass


def _definition_url(model: str | None) -> str:
    """TEXT2SQL_MCP_URL 은 MCP 주소(.../mcp)다. 같은 서버의 /definition[/모델] 로 바꾼다."""
    base = os.getenv("TEXT2SQL_MCP_URL", DEFAULT_URL).rstrip("/").removesuffix("/mcp")
    return f"{base}/definition" + (f"/{quote(model, safe='')}" if model else "")


def _server_code(response: httpx2.Response) -> str:
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" {code}" if isinstance(code, str) and code.isidentifier() else ""


def fetch_definition(model: str | None = None, *, datasets: list[str] = (), tables: list[str] = (),
                     metrics: list[str] = (), with_definition: bool = True,
                     transport: httpx2.BaseTransport | None = None) -> dict:
    """{"model", "version_id", "name", "revision", "defined_tables", "definition"?, ...}.
    definition 은 요청한 이름이 닿는 데이터셋만 담은 Ossie 문서다. 실패는 원인별 안내를 담은 GraphClientError.
    토큰 값은 메시지에 넣지 않는다."""
    token = os.getenv("TEXT2SQL_MCP_TOKEN")
    if not token:
        raise GraphClientError("TEXT2SQL_MCP_TOKEN 이 없다. Graph MCP 인증을 설정한다")
    body = {"datasets": sorted(set(datasets)), "tables": sorted(set(tables)), "metrics": sorted(set(metrics)),
            "definition": with_definition}
    try:
        with httpx2.Client(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0),
                           transport=transport) as http:
            response = http.post(_definition_url(model), json=body)
    except httpx2.TimeoutException as error:
        raise GraphClientError("Graph 서버가 30초 안에 응답하지 않았다. 잠시 뒤 다시 시도한다") from error
    except httpx2.TransportError as error:
        raise GraphClientError(f"Graph 서버에 연결하지 못했다({type(error).__name__}). TEXT2SQL_MCP_URL 주소와 네트워크를 확인한다") from error
    except Exception as error:  # 잘못된 주소·헤더 값 등. 원문에 토큰이 섞일 수 있어 종류만 적는다
        raise GraphClientError(f"Graph 서버 호출 실패: {type(error).__name__}") from error
    status = response.status_code
    if status in (401, 403):
        raise GraphClientError(f"Graph 서버가 인증을 거절했다({status}). TEXT2SQL_MCP_TOKEN 값을 확인한다")
    if status == 404 and _server_code(response):
        raise GraphClientError(f"활성 모델을 찾지 못했다(404{_server_code(response)}). GRAPH_MODEL 이름을 확인한다")
    if status in (404, 405):
        raise GraphClientError(f"Graph 서버에 /definition 경로가 없다({status}). Graph 서버를 0.4.0 이상으로 올린다")
    if status == 400:
        raise GraphClientError(f"Graph 서버가 요청을 거절했다(400{_server_code(response)}). 명령에 쓴 이름을 줄여 다시 시도한다")
    if status >= 500:
        raise GraphClientError(f"Graph 서버 쪽 오류다({status}{_server_code(response)}). 서버 상태와 Neo4j 연결을 확인한다")
    if status != 200:
        raise GraphClientError(f"Graph 서버가 예상하지 못한 응답을 했다({status}). TEXT2SQL_MCP_URL 주소를 확인한다")
    try:
        out = response.json()
    except ValueError as error:
        raise GraphClientError("Graph 서버 응답이 JSON 이 아니다. TEXT2SQL_MCP_URL 주소를 확인한다") from error
    if not isinstance(out, dict) or (with_definition and not isinstance(out.get("definition"), dict)):
        raise GraphClientError("Graph 서버 응답에 실행용 정의가 없다")
    if not out.get("model") or not isinstance(out.get("version_id"), str) or not out["version_id"]:
        raise GraphClientError("Graph 서버 응답에 모델 또는 활성 버전이 없다")
    if model and out["model"] != model:
        raise GraphClientError("Graph 서버 응답 모델이 요청과 다르다")
    return out
