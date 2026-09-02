from __future__ import annotations

import json
from pathlib import Path

from selene.qualification.oos_status import write_oos_status


def test_oos_status_blocked_zero_trades(tmp_path: Path):
    path = write_oos_status(tmp_path)
    payload = json.loads(path.read_text())
    assert payload["n_trades"] == 0
    assert payload["required_trades"] == 100
    assert payload["missing_trades"] == 100
    assert payload["verdict"] == "BLOCKED_INSUFFICIENT_DATA"
    assert payload["I_HAVE_OOS_EVIDENCE"] is False
    assert payload["historical_oi_source"] == "OWNER_BLOCKED"
