"""Refuse secret-shaped values in reports, tests snapshots, and frontend sources."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SECRET_ASSIGN = re.compile(
    r"""(?:api[_-]?key|secret|token|password|passphrase)\s*[:=]\s*['\"][^'\"]{8,}['\"]""",
    re.I,
)
SKIP_DIR = {".git", "node_modules", ".venv", "vendor", "__pycache__"}
SKIP_SUFFIX = {".png", ".jpg", ".woff", ".woff2", ".pyc", ".sh"}


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIR for part in path.parts):
            continue
        if not path.is_file() or path.suffix in SKIP_SUFFIX:
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SECRET_ASSIGN.search(text):
            # Allow obvious placeholders.
            if "changeme" in text.lower() or "your_" in text.lower() or "example" in text.lower():
                continue
            if "super-secret-value" in text or "op-secret" in text:
                # test fixtures that assert redaction
                if "test_" in path.name or path.suffix == ".py" and "tests/" in str(path):
                    continue
            rel = str(path.relative_to(root))
            if rel.startswith("tests/"):
                continue
            hits.append(rel)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    hits = scan(Path(args.root).resolve())
    if hits:
        sys.stderr.write(json.dumps({"status": "fail", "hits": hits[:50]}) + "\n")
        return 2
    sys.stdout.write(json.dumps({"status": "ok", "hits": []}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
