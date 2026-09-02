from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHRASE = "All rights reserved. No license is granted at this time."


def test_readme_states_all_rights_reserved():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert PHRASE in text
    assert "MIT License" not in text
    assert "freely copy" not in text.lower()
    assert "free to modify" not in text.lower()


def test_license_file_does_not_grant():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "OWNER_BLOCKED" in text
    assert "no license is granted" in text.lower()
