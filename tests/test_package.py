"""claude.ai 업로드 파일의 필수 구성과 금지 경로를 확인한다."""

import json
import runpy
from pathlib import Path
from stat import S_IFLNK, S_IFMT
from zipfile import ZipFile

import yaml


def test_upload_archive(tmp_path):
    root = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(root / "scripts" / "build_plugin.py"), run_name="test")
    archive = tmp_path / "text2sql.plugin"
    build["main"](archive)

    with ZipFile(archive) as zipped:
        names = zipped.namelist()
        assert names.count(".claude-plugin/plugin.json") == 1
        assert {
            "scripts/t2s", "t2s/cli.py", "t2s/concepts.py", "t2s/measures.py",
            "skills/ask/SKILL.md", "skills/semantic-sync/SKILL.md",
            "skills/graph-metadata/SKILL.md", "skills/define-metric/SKILL.md",
            "references/definition-format.md", ".mcp.json", "hooks/check_definition.py",
        } <= set(names)
        assert "plugin.json" not in names
        assert ".claude-plugin/marketplace.json" not in names
        assert not any(name.startswith("bin/") for name in names)
        assert not any(S_IFMT(info.external_attr >> 16) == S_IFLNK for info in zipped.infolist())
        assert zipped.testzip() is None

        skill = zipped.read("skills/define-metric/SKILL.md").decode("utf-8")
        frontmatter = yaml.safe_load(skill.split("---", 2)[1])
        assert frontmatter["name"] == "define-metric"
        assert "승인" in frontmatter["description"]

        claude_mcp = json.loads(zipped.read(".mcp.json"))
        claude_server = claude_mcp["mcpServers"]["text2sql-graph"]
        assert claude_server["headers"]["Authorization"] == "Bearer ${user_config.graph_mcp_token}"


def test_remote_graph_mcp_configuration():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "mcp.json").read_text())
    server = config["mcpServers"]["text2sql-graph"]
    assert server["type"] == "streamable-http"
    assert server["url"] == "https://text2sql-graph-mcp-production.up.railway.app/mcp"
    assert server["extensions"]["com.openai"]["auth"] == {
        "type": "api_key", "headerScheme": "bearer",
    }
    assert "headers" not in server

    claude_server = json.loads((root / ".mcp.json").read_text())["mcpServers"]["text2sql-graph"]
    assert claude_server["type"] == server["type"]
    assert claude_server["url"] == server["url"]

    claude = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    token = claude["userConfig"]["graph_mcp_token"]
    assert token["type"] == "string" and token["sensitive"] is True
