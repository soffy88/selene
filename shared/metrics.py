"""Dependency-free Prometheus text-exposition helpers (optimization item #12).

prometheus_client is not a dependency here, so this renders the Prometheus
text exposition format from plain dicts. Services expose a /metrics endpoint
that calls render_prometheus(...); a Prometheus server scrapes them once it (and
the exporters) are added to docker-compose.

Format ref: https://prometheus.io/docs/instrumenting/exposition_formats/
"""
from __future__ import annotations

from typing import Iterable, Optional


def _fmt_labels(labels: Optional[dict]) -> str:
    if not labels:
        return ""
    inner = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
        for k, v in sorted(labels.items())
    )
    return "{" + inner + "}"


def render_metric(name: str, value, *, mtype: str = "gauge",
                  help_text: str = "", labels: Optional[dict] = None) -> str:
    """Render a single metric (with HELP/TYPE header) to exposition text."""
    lines = []
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {mtype}")
    lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
    return "\n".join(lines)


def _num(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return repr(float(value)) if isinstance(value, float) else str(int(value))


def render_prometheus(metrics: Iterable[dict]) -> str:
    """Render a sequence of metric dicts.

    Each dict: {"name": str, "value": number|bool, "type": "gauge"|"counter",
                "help": str (optional), "labels": dict (optional)}.
    Returns the full exposition body (trailing newline included).
    """
    blocks = []
    for m in metrics:
        blocks.append(render_metric(
            m["name"], m["value"],
            mtype=m.get("type", "gauge"),
            help_text=m.get("help", ""),
            labels=m.get("labels"),
        ))
    return "\n".join(blocks) + "\n"
