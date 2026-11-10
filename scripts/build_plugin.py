"""Build the claude.ai upload archive without local-only files."""

from pathlib import Path
from stat import S_IFREG
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "text2sql.plugin"
FILES = [
    ROOT / ".claude-plugin" / "plugin.json",
    ROOT / "LICENSE",
    ROOT / "pyproject.toml",
    ROOT / "scripts" / "t2s",
    *sorted((ROOT / "agents").glob("*.md")),
    *sorted((ROOT / "hooks").glob("*.json")),
    *sorted((ROOT / "hooks").glob("*.py")),
    *sorted((ROOT / "references").glob("*.md")),
    *sorted((ROOT / "skills").glob("*/SKILL.md")),
    *sorted((ROOT / "t2s").glob("*.py")),
]


def main(output: Path = OUTPUT) -> None:
    if len(FILES) != len(set(FILES)) or any(not path.is_file() or path.is_symlink() for path in FILES):
        raise SystemExit("패키지 파일이 없거나 링크가 포함돼 있습니다")

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w") as archive:
        for path in FILES:
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith("bin/") or ".." in Path(relative).parts:
                raise SystemExit(f"허용하지 않는 경로: {relative}")
            info = ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (S_IFREG | (0o755 if relative == "scripts/t2s" else 0o644)) << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    print(output)


if __name__ == "__main__":
    main()
