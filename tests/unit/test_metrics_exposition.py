"""Prometheus exposition formatter tests (optimization item #12)."""
from shared.metrics import render_metric, render_prometheus


def test_gauge_basic():
    out = render_metric("selene_up", 1, help_text="up", mtype="gauge")
    assert "# HELP selene_up up" in out
    assert "# TYPE selene_up gauge" in out
    assert "selene_up 1" in out


def test_bool_renders_as_1_0():
    assert render_metric("x", True).endswith("x 1")
    assert render_metric("y", False).endswith("y 0")


def test_float_value():
    out = render_metric("eq", 1234.5)
    assert "eq 1234.5" in out


def test_labels_sorted_and_escaped():
    out = render_metric("m", 1, labels={"b": "2", "a": 'has"quote'})
    # labels sorted: a then b
    assert 'a="has\\"quote"' in out
    assert out.index('a="') < out.index('b="')


def test_render_prometheus_multiple_and_trailing_newline():
    body = render_prometheus([
        {"name": "selene_up", "value": 1, "help": "up"},
        {"name": "selene_open_positions", "value": 3, "type": "gauge"},
        {"name": "selene_halted", "value": False},
    ])
    assert body.endswith("\n")
    assert "selene_up 1" in body
    assert "selene_open_positions 3" in body
    assert "selene_halted 0" in body
