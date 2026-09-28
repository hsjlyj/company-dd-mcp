from __future__ import annotations

from company_dd.models import Report, Subject
from company_dd.rules import SweepScope, evaluate


def test_keyed_and_manual_coverage_can_reach_green() -> None:
    """受控回归：覆盖所有红线后才允许从黄/未验证升级为绿。"""
    rep = Report(subject=Subject(query="x", jurisdiction="CN", listing="A股 000001"))
    keyed = {
        rid: {"source": "tianyancha_cli", "source_label": "tyc", "checked": ["test"], "evidence": []}
        for rid in ("RL-01", "RL-02", "RL-05", "RL-12")
    }
    sweep = SweepScope(
        source_id="cninfo", source_label="巨潮", scanned=100, ok=True,
        date_from="2026-01-01", date_to="2026-06-01", window_days=180,
        covers_window=True,
    )
    rules, verdict, _ = evaluate(
        rep,
        announcements=[{"title": "普通公告", "date": "2026-05-01", "url": "https://example.test"}],
        sweep=sweep,
        keyed_hits=keyed,
    )
    assert verdict == "green"
    assert all(r.status == "clear" for r in rules if r.severity == "red_line")
