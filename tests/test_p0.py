from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_dd.config import Config
from company_dd.http import HttpClient
from company_dd.manual import ManualFinding, ManualStore, apply_manual
from company_dd.models import Report, Subject
from company_dd.sources.tyc_cli import TycCliSource, rule_hits


def test_tyc_rule_hits_distinguishes_clear_from_hit() -> None:
    collected = {
        "executed": {"status": "ok", "label": "被执行人", "rules": ["RL-01"], "hits": []},
        "dishonest": {"status": "ok", "label": "失信被执行人", "rules": ["RL-01"], "hits": [{"case": "A-1"}]},
        "business_exception": {"status": "ok", "label": "经营异常名录", "rules": ["RL-02"], "hits": []},
    }
    rows = rule_hits(collected)
    assert rows["RL-01"]["checked"] == ["被执行人", "失信被执行人"]
    assert rows["RL-01"]["evidence"][0]["item"]["case"] == "A-1"
    assert rows["RL-02"]["evidence"] == []


def test_tyc_without_login_is_explicit_needs_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = Config(home=tmp_path)
    src = TycCliSource(cfg, HttpClient(cfg))
    monkeypatch.setattr(src, "binary", lambda: "/usr/bin/tyc")
    assert src.ready()[0] is False
    st = src.probe()
    assert st.status == "needs_key"
    assert "未登录" in st.detail


def test_manual_store_requires_trace_and_replaces_same_rule(tmp_path: Path) -> None:
    store = ManualStore(tmp_path)
    with pytest.raises(Exception):
        store.add("r1", ManualFinding(rule_id="RL-01", outcome="clear", operator="u", method="GSXT"))
    store.add("r1", ManualFinding(rule_id="RL-01", outcome="clear", operator="u", method="GSXT", url="https://example.test"))
    store.add("r1", ManualFinding(rule_id="RL-01", outcome="hit", operator="u", method="GSXT", url="https://example.test/2"))
    rows = store.load("r1")
    assert len(rows) == 1 and rows[0]["outcome"] == "hit"


def test_apply_manual_changes_only_named_rule() -> None:
    rep = Report(subject=Subject(query="x"))
    rules = [
        {"rule_id": "RL-01", "title": "执行", "severity": "red_line", "status": "unverified"},
        {"rule_id": "RL-02", "title": "异常", "severity": "red_line", "status": "unverified"},
    ]
    from company_dd.manual import rules_from_dict
    out = apply_manual(rules_from_dict(rules), [{
        "rule_id": "RL-01", "outcome": "clear", "operator": "u", "method": "GSXT",
        "url": "https://example.test", "checked_at": "2026-01-01T00:00:00+08:00",
        "finding_id": "f1", "attachment": "", "note": "无记录",
    }])
    assert next(r for r in out if r.rule_id == "RL-01").status == "clear"
    assert next(r for r in out if r.rule_id == "RL-02").status == "unverified"
