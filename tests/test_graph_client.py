"""t2s 가 Graph 서버의 /snapshot 에서 활성 정의를 받고, 실패를 원인별로 알리는지. 실제 서버에는 접속하지 않는다."""
import httpx2
import pytest

from t2s import graph_client
from t2s.graph_client import GraphClientError, fetch_model

TOKEN = "secret-token-" + "x" * 32
SNAPSHOT = {"tenant": "olist", "model": "olist_sales", "version_id": "v1", "definition": {"name": "olist_sales", "datasets": []}}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("TEXT2SQL_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("TEXT2SQL_MCP_URL", raising=False)
    return monkeypatch


def serve(status=200, body=SNAPSHOT, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body) if not isinstance(body, str) else httpx2.Response(status, text=body)
    return httpx2.MockTransport(handler)


def test_reads_snapshot_route_with_bearer_token(token):
    seen = []
    assert fetch_model(transport=serve(seen=seen)) == SNAPSHOT
    named_model = {**SNAPSHOT, "model": "모델/이름 1"}
    assert fetch_model("모델/이름 1", transport=serve(body=named_model, seen=seen)) == named_model
    token.setenv("TEXT2SQL_MCP_URL", "http://localhost:8000/mcp/")
    fetch_model("olist_sales", transport=serve(seen=seen))
    assert [str(r.url) for r in seen] == [
        "https://text2sql-graph-mcp-production.up.railway.app/snapshot",
        "https://text2sql-graph-mcp-production.up.railway.app/snapshot/%EB%AA%A8%EB%8D%B8%2F%EC%9D%B4%EB%A6%84%201",
        "http://localhost:8000/snapshot/olist_sales",
    ]
    assert all(r.method == "GET" and r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen)


def test_rejects_a_snapshot_from_another_model(token):
    with pytest.raises(GraphClientError, match="응답 모델이 요청과 다르다"):
        fetch_model("another_model", transport=serve())


def test_missing_token_never_calls_the_server(token):
    token.delenv("TEXT2SQL_MCP_TOKEN")
    seen = []
    with pytest.raises(GraphClientError, match="TEXT2SQL_MCP_TOKEN 이 없다"):
        fetch_model(transport=serve(seen=seen))
    assert seen == []


def fail(request):
    raise httpx2.ConnectError("connection refused", request=request)


def slow(request):
    raise httpx2.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize("transport, message", [
    (serve(401, "Unauthorized"), r"인증을 거절했다\(401\).*TEXT2SQL_MCP_TOKEN 값을 확인"),
    (serve(403, "Forbidden"), r"인증을 거절했다\(403\)"),
    (serve(404, {"error": {"code": "NO_ACTIVE_VERSION", "message": "No active graph for this model"}}), r"찾지 못했다\(404\).*GRAPH_MODEL.*0\.3\.0"),
    (serve(404, "Not Found"), r"찾지 못했다\(404\)"),
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
        fetch_model(transport=transport)
    assert TOKEN not in str(error.value) and "secret" not in str(error.value)


def test_no_mcp_client_dependency():
    """스냅샷은 MCP 도구가 아니다. MCP 클라이언트 세션으로 되돌아가지 않게 한다."""
    assert not hasattr(graph_client, "ClientSession") and "call_tool" not in open(graph_client.__file__, encoding="utf-8").read()
