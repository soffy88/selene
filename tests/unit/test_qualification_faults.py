from __future__ import annotations

from selene.qualification.faults import run_faults


def test_faults_duplicate_timeout_partial_restart():
    cases = {c.name: c for c in run_faults()}
    assert cases["duplicate_message"].status == "PASS"
    assert cases["duplicate_message"].detail["submits"] == 1
    assert cases["timeout_no_resubmit"].status == "PASS"
    assert cases["out_of_order_fills"].status == "PASS"
    assert cases["partial_fill"].status == "PASS"
    assert cases["process_restart_inflight"].status == "PASS"
    assert cases["process_restart_open"].status == "PASS"
    assert cases["ledger_restart"].status == "PASS"
    assert all(c.status == "PASS" for c in cases.values())
