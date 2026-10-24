"""One-shot TUI-style report: a fixed-width table printed once and done."""
from __future__ import annotations

import time
from typing import Dict, List

from .classify import FailureClass

_COLOR = {
    FailureClass.HEALTHY.value: "32",        # green
    FailureClass.RATE_LIMITED.value: "33",   # yellow
    FailureClass.SERVER_ERROR.value: "33",   # yellow
    FailureClass.NETWORK_ERROR.value: "33",  # yellow
}
# broken classes (invalid / forbidden / quota / unknown) default to red (31)

_COLUMNS = [
    ("PROVIDER", 12),
    ("KEY", 22),
    ("STATUS", 18),
    ("LATENCY(ms)", 11),
    ("FAILS", 5),
    ("DETAIL", 28),
]


def _pad(text: str, width: int) -> str:
    return text if len(text) >= width else text + " " * (width - len(text))


def _cell(text: str, width: int, color: str | None, colored: bool) -> str:
    """Pad to width first, then optionally wrap the whole cell in color."""
    padded = _pad(text, width)
    return _colorize(padded, color, colored) if color and colored else padded


def _colorize(text: str, code: str, enabled: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if enabled else text


def _fmt_latency(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def _fmt_time(value: float | None) -> str:
    if value is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def render_report(snapshot: Dict, colored: bool = False) -> str:
    """Render the pool snapshot as a plain-text table.

    ``snapshot`` is ``pool.KeyPool.snapshot()`` output; key names are
    already masked/labelled upstream, so raw keys never reach here.
    """
    lines: List[str] = []
    header = "  ".join(_pad(title, width) for title, width in _COLUMNS)
    separator = "  ".join("-" * width for _, width in _COLUMNS)

    total = healthy = 0
    for provider_name in sorted(snapshot):
        provider = snapshot[provider_name]
        for key in provider.keys:
            total += 1
            if key.healthy:
                healthy += 1
            detail = key.last_error or "checked " + _fmt_time(key.last_checked)
            lines.append(
                "  ".join(
                    [
                        _cell(provider.name, _COLUMNS[0][1], None, colored),
                        _cell(key.key_name, _COLUMNS[1][1], None, colored),
                        _cell(
                            key.cls,
                            _COLUMNS[2][1],
                            _COLOR.get(key.cls, "31"),
                            colored,
                        ),
                        _cell(_fmt_latency(key.last_latency_ms), _COLUMNS[3][1], None, colored),
                        _cell(str(key.total_failures), _COLUMNS[4][1], None, colored),
                        _cell(detail[: _COLUMNS[5][1]], _COLUMNS[5][1], None, colored),
                    ]
                ).rstrip()
            )

    out = [header, separator] + lines
    summary = f"healthy {healthy}/{total} keys across {len(snapshot)} provider(s)"
    out.append("")
    out.append(
        _colorize(
            summary,
            "32" if total and healthy == total else "33",
            colored,
        )
    )
    return "\n".join(out)
