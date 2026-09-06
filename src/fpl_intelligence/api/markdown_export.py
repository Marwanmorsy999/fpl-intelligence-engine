"""Phase 5 — decision report markdown export.

Converts a :class:`~fpl_intelligence.squad.models.DecisionReport`-like
object (a dict, in practice) into a self-contained Markdown
document. The output is stable: re-running the export on the same
input produces byte-identical output, so users can diff or sign it.

Sections
--------

1. Header — entry id, gameweek, captain, model version, generated_at
2. Starting XI (table) — player, position, xPTS, captain flag
3. Bench (table) — same shape
4. Captain spotlight — captain xPTS, vice xPTS, captaincy pick
5. Transfer recommendations — in/out pairs
6. Chip recommendation — name, expected lift, rationale
7. Data sources & model version footer

The export deliberately omits any sensitive fields (session id, user
ids) and never includes raw model coefficients. It is safe to share.

Pure function — no I/O. The route handler composes the input dict
and streams the result as ``text/markdown``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def render_decision_markdown(report: Mapping[str, Any]) -> str:
    """Render a decision report as Markdown.

    The report is expected to follow the shape produced by
    :class:`DecisionOptimizerBridge.generate_decisions` after
    ``.model_dump(mode="json")``: a dict with ``starting_xi``,
    ``bench_order``, ``captain``, ``vice_captain``, ``transfers``,
    ``chip_recommendation``, and an optional ``_meta`` block.
    """
    parts: list[str] = []
    _h1(parts, _title(report))
    _section_meta(parts, report)
    _section_starting_xi(parts, report)
    _section_bench(parts, report)
    _section_captain(parts, report)
    _section_transfers(parts, report)
    _section_chip(parts, report)
    _section_footer(parts, report)
    return "".join(parts)


def _title(report: Mapping[str, Any]) -> str:
    entry_id = report.get("entry_id") or report.get("entry") or "demo"
    gameweek = report.get("gameweek", "?")
    return f"FPL Decision Report — entry {entry_id} (GW{gameweek})"


def _h1(parts: list[str], title: str) -> None:
    parts.append(f"# {title}\n\n")


def _section_meta(parts: list[str], report: Mapping[str, Any]) -> None:
    meta = report.get("_meta") or {}
    generated = report.get("generated_at") or meta.get("built_at")
    if isinstance(generated, (int, float)):
        generated = datetime.fromtimestamp(generated, tz=UTC).isoformat()
    lines = [
        "| field | value |",
        "|---|---|",
        f"| generated_at | {generated or 'unknown'} |",
        f"| model_version | {meta.get('model_version', 'unknown')} |",
        f"| data_snapshot_id | {meta.get('data_snapshot_id', 'unknown')} |",
    ]
    refresh_state = report.get("refresh_state")
    if refresh_state:
        lines.append(f"| refresh_state | {refresh_state} |")
    parts.append("\n".join(lines) + "\n\n")


def _section_starting_xi(parts: list[str], report: Mapping[str, Any]) -> None:
    xi = report.get("starting_xi") or []
    if not xi:
        return
    parts.append("## Starting XI\n\n")
    parts.append("| slot | player | position | xPTS | captain |\n|---|---|---|---|---|\n")
    captain_id = report.get("captain") or report.get("captain_id")
    for i, slot in enumerate(xi, start=1):
        player_id = _slot_player_id(slot)
        name = _slot_name(slot, default=f"player {player_id}")
        position = _slot_position(slot)
        xpcts = _slot_xpcts(slot)
        is_captain = "✓" if player_id == captain_id else ""
        parts.append(f"| {i} | {name} | {position} | {xpcts} | {is_captain} |\n")
    parts.append("\n")


def _section_bench(parts: list[str], report: Mapping[str, Any]) -> None:
    bench = report.get("bench_order") or []
    if not bench:
        return
    parts.append("## Bench (auto-sub order)\n\n")
    parts.append("| slot | player | position | xPTS |\n|---|---|---|---|\n")
    for i, slot in enumerate(bench, start=1):
        player_id = _slot_player_id(slot)
        name = _slot_name(slot, default=f"player {player_id}")
        position = _slot_position(slot)
        xpcts = _slot_xpcts(slot)
        parts.append(f"| {i} | {name} | {position} | {xpcts} |\n")
    parts.append("\n")


def _section_captain(parts: list[str], report: Mapping[str, Any]) -> None:
    captain_id = report.get("captain") or report.get("captain_id")
    vice_id = report.get("vice_captain") or report.get("vice_captain_id")
    if captain_id is None and vice_id is None:
        return
    parts.append("## Captaincy\n\n")
    parts.append(f"- **Captain:** player {captain_id}\n")
    parts.append(f"- **Vice-captain:** player {vice_id}\n\n")


def _section_transfers(parts: list[str], report: Mapping[str, Any]) -> None:
    transfers = report.get("transfers") or []
    if not transfers:
        return
    parts.append("## Transfer Recommendations\n\n")
    parts.append("| direction | player | xPTS |\n|---|---|---|\n")
    for tr in transfers:
        for label, slot in (("out", tr.get("out") or {}), ("in", tr.get("in") or {})):
            player_id = _slot_player_id(slot)
            name = _slot_name(slot, default=f"player {player_id}")
            xpcts = _slot_xpcts(slot)
            parts.append(f"| {label} | {name} | {xpcts} |\n")
    parts.append("\n")


def _section_chip(parts: list[str], report: Mapping[str, Any]) -> None:
    chip = report.get("chip_recommendation")
    if not chip:
        return
    parts.append("## Chip Recommendation\n\n")
    name = chip.get("name") or chip.get("chip") or "—"
    expected = chip.get("expected_value") or chip.get("net_value")
    parts.append(f"- **Chip:** {name}\n")
    if expected is not None:
        parts.append(f"- **Expected value:** {expected}\n")
    rationale = chip.get("rationale")
    if rationale:
        parts.append(f"- **Rationale:** {rationale}\n")
    parts.append("\n")


def _section_footer(parts: list[str], report: Mapping[str, Any]) -> None:
    parts.append("---\n\n")
    parts.append(
        "Generated by FPL Intelligence Engine. "
        "Predictions are advisory; transfer decisions remain yours.\n"
    )


def _slot_player_id(slot: Any) -> int | str:
    if isinstance(slot, Mapping):
        return slot.get("player_id") or slot.get("element_id") or slot.get("id") or "?"
    return getattr(slot, "player_id", None) or getattr(slot, "element_id", None) or "?"


def _slot_name(slot: Any, default: str = "") -> str:
    if isinstance(slot, Mapping):
        return slot.get("name") or slot.get("web_name") or default
    return getattr(slot, "name", None) or getattr(slot, "web_name", None) or default


def _slot_position(slot: Any) -> str:
    if isinstance(slot, Mapping):
        pos = slot.get("position") or slot.get("element_type")
        return str(pos) if pos is not None else "—"
    return str(getattr(slot, "position", None) or getattr(slot, "element_type", None) or "—")


def _slot_xpcts(slot: Any) -> str:
    if isinstance(slot, Mapping):
        xp = slot.get("expected_points") or slot.get("xpcts") or slot.get("xPTS")
        return f"{xp:.2f}" if isinstance(xp, (int, float)) else "—"
    xp = getattr(slot, "expected_points", None) or getattr(slot, "xpcts", None)
    return f"{xp:.2f}" if isinstance(xp, (int, float)) else "—"
