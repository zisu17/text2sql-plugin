"""claude.ai 업로드 파일의 필수 구성과 금지 경로를 확인한다."""

import runpy
from pathlib import Path
from stat import S_IFLNK, S_IFMT
from zipfile import ZipFile


def test_upload_archive(tmp_path):
    root = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(root / "scripts" / "build_plugin.py"), run_name="test")
    archive = tmp_path / "text2sql.plugin"
    build["main"](archive)

    with ZipFile(archive) as zipped:
        names = zipped.namelist()
        assert names.count(".claude-plugin/plugin.json") == 1
        assert {"scripts/t2s", "t2s/cli.py", "skills/ask/SKILL.md", "hooks/check_definition.py"} <= set(names)
        assert "plugin.json" not in names
        assert ".claude-plugin/marketplace.json" not in names
        assert not any(name.startswith("bin/") for name in names)
        assert not any(S_IFMT(info.external_attr >> 16) == S_IFLNK for info in zipped.infolist())
        assert zipped.testzip() is None
