"""t2s 가 Graph 서버의 /definition 에서 명령에 필요한 정의만 받고, 실패를 원인별로 알리는지. 실제 서버에는 접속하지 않는다."""
import json

import httpx2
import pytest

from t2s import graph_client
from t2s.graph_client import GraphClientError, fetch_definition

TOKEN = "secret-token-" + "x" * 32
ANSWER = {"tenant": "olist", "model": "olist_sales", "version_id": "v1", "name": "olist_sales", "revision": 3,
          "defined_tables": ["orders"], "definition": {"name": "olist_sales", "datasets": []}}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("TEXT2SQL_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("TEXT2SQL_MCP_URL", raising=False)
    return monkeypatch


def serve(status=200, body=ANSWER, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body) if not isinstance(body, str) else httpx2.Response(status, text=body)
    return httpx2.MockTransport(handler)


def test_posts_only_the_names_a_command_uses(token):
    seen = []
    got = fetch_definition(datasets=["order_lines", "order_lines"], tables=["orders"], metrics=["revenue"],
                           transport=serve(seen=seen))
    assert got == ANSWER
    named = {**ANSWER, "model": "모델/이름 1"}
    assert fetch_definition("모델/이름 1", transport=serve(body=named, seen=seen)) == named
    token.setenv("TEXT2SQL_MCP_URL", "http://localhost:8000/mcp/")
    meta = {k: v for k, v in ANSWER.items() if k != "definition"}
    assert fetch_definition("olist_sales", tables=["orders"], with_definition=False, transport=serve(body=meta, seen=seen)) == meta
    assert [str(r.url) for r in seen] == [
        "https://text2sql-graph-mcp-production.up.railway.app/definition",
        "https://text2sql-graph-mcp-production.up.railway.app/definition/%EB%AA%A8%EB%8D%B8%2F%EC%9D%B4%EB%A6%84%201",
        "http://localhost:8000/definition/olist_sales",
    ]
    assert all(r.method == "POST" and r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen)
    assert json.loads(seen[0].content) == {"datasets": ["order_lines"], "tables": ["orders"], "metrics": ["revenue"],
                                           "definition": True}
    assert json.loads(seen[2].content)["definition"] is False


def test_rejects_an_answer_from_another_model(token):
    with pytest.raises(GraphClientError, match="응답 모델이 요청과 다르다"):
        fetch_definition("another_model", transport=serve())


def test_missing_token_never_calls_the_server(token):
    token.delenv("TEXT2SQL_MCP_TOKEN")
    seen = []
    with pytest.raises(GraphClientError, match="TEXT2SQL_MCP_TOKEN 이 없다"):
        fetch_definition(transport=serve(seen=seen))
    assert seen == []


def fail(request):
    raise httpx2.ConnectError("connection refused", request=request)


def slow(request):
    raise httpx2.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize("transport, message", [
    (serve(401, "Unauthorized"), r"인증을 거절했다\(401\).*TEXT2SQL_MCP_TOKEN 값을 확인"),
    (serve(403, "Forbidden"), r"인증을 거절했다\(403\)"),
    (serve(404, {"error": {"code": "NO_ACTIVE_VERSION", "message": "No active graph for this model"}}),
     r"찾지 못했다\(404 NO_ACTIVE_VERSION\).*GRAPH_MODEL"),
    (serve(404, "Not Found"), r"/definition 경로가 없다\(404\).*0\.4\.0"),
    (serve(405, "Method Not Allowed"), r"/definition 경로가 없다\(405\)"),
    (serve(400, {"error": {"code": "INVALID_INPUT", "message": "bad"}}), r"요청을 거절했다\(400 INVALID_INPUT\)"),
    (serve(503, {"error": {"code": "DB_UNAVAILABLE", "message": "Neo4j query failed or timed out"}}), r"서버 쪽 오류다\(503 DB_UNAVAILABLE\)"),
    (serve(500, "Internal Server Error"), r"서버 쪽 오류다\(500\)"),
    (serve(302, ""), r"예상하지 못한 응답을 했다\(302\)"),
    (serve(200, "<html>"), "JSON 이 아니다"),
    (serve(200, {"version_id": "v1"}), "실행용 정의가 없다"),
    (serve(200, {"model": "olist_sales", "definition": {}}), "모델 또는 활성 버전이 없다"),
    (httpx2.MockTransport(fail), r"연결하지 못했다\(ConnectError\).*TEXT2SQL_MCP_URL"),
    (httpx2.MockTransport(slow), "30초 안에 응답하지 않았다"),
])
def test_failures_name_their_cause_without_the_token(token, transport, message):
    with pytest.raises(GraphClientError, match=message) as error:
        fetch_definition(transport=transport)
    assert TOKEN not in str(error.value) and "secret" not in str(error.value)


def test_no_mcp_client_and_no_whole_model():
    """정의는 MCP 도구가 아니다. 모델 전체를 받던 /snapshot 으로 되돌아가지 않는다."""
    source = open(graph_client.__file__, encoding="utf-8").read()
    assert not hasattr(graph_client, "ClientSession") and "call_tool" not in source and "/snapshot" not in source
