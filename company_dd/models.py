"""数据模型：字段、证据、主体、规则结论、报告。

核心不变式（写进类型里，不靠自觉）：
  * Field.status == "verified" ⇒ Field.evidence 非空。
  * blocked / needs_key / unverified 的字段必须带 note 说明原因。
  * 报告 verdict == "green" ⇒ 所有红线规则 status == "clear"。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))

# 字段状态
VERIFIED = "verified"        # 有证据，可直接引用
PARTIAL = "partial"          # 只有部分来源验证（例如仅第三方聚合）
UNVERIFIED = "unverified"    # 来源可用但查不到该字段
BLOCKED = "blocked"          # 来源被 WAF/验证码拦截，查不了
NEEDS_KEY = "needs_key"      # 需要商业数据源的 Key 才能查
NOT_APPLICABLE = "not_applicable"

OPEN_STATUSES = (UNVERIFIED, BLOCKED, NEEDS_KEY)

RULE_HIT = "hit"
RULE_CLEAR = "clear"
RULE_UNVERIFIED = "unverified"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


@dataclass
class Evidence:
    source_id: str
    source_label: str
    url: str
    fetched_at: str
    pointer: str = ""
    sha256: str = ""
    raw_path: str = ""
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Field:
    name: str
    label: str
    value: Any = None
    status: str = UNVERIFIED
    note: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d


@dataclass
class SourceStatus:
    source_id: str
    label: str
    kind: str                # official | third_party | aggregator | keyed
    jurisdiction: str
    status: str              # ok | blocked | needs_key | error | skipped
    http_status: int | None = None
    url: str = ""
    detail: str = ""
    fetched_at: str = ""
    manual_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskSignal:
    level: str               # red | yellow | info
    title: str
    detail: str
    source_id: str = ""
    evidence_url: str = ""
    date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuleResult:
    rule_id: str
    title: str
    severity: str            # red_line | warning
    status: str              # hit | clear | unverified
    reason: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    manual_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d


@dataclass
class Subject:
    query: str
    name: str = ""
    jurisdiction: str = "unknown"   # CN | US | HK | GLOBAL
    listing: str = ""
    ids: dict[str, str] = field(default_factory=dict)  # credit_code / ticker / cik / lei / qid
    aliases: list[str] = field(default_factory=list)
    confidence: str = "low"         # high | medium | low
    disambiguation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    subject: Subject
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    generated_at: str = field(default_factory=now_iso)
    fields: dict[str, Field] = field(default_factory=dict)
    signals: list[RiskSignal] = field(default_factory=list)
    rules: list[RuleResult] = field(default_factory=list)
    sources: list[SourceStatus] = field(default_factory=list)
    manual_steps: list[str] = field(default_factory=list)
    verdict: str = "inconclusive"   # red | yellow | green | inconclusive
    verdict_reason: str = ""
    notes: list[str] = field(default_factory=list)

    # ---- 写入口 ----
    def put(self, name: str, label: str, value: Any, *, status: str = VERIFIED,
            evidence: list[Evidence] | None = None, note: str = "") -> Field:
        ev = list(evidence or [])
        if status == VERIFIED and not ev:
            raise ValueError(f"字段 {name} 标为 verified 但没有任何证据")
        if status in OPEN_STATUSES and not note:
            raise ValueError(f"字段 {name} 状态为 {status}，必须写明原因(note)")
        f = Field(name=name, label=label, value=value, status=status, note=note, evidence=ev)
        self.fields[name] = f
        return f

    def gaps(self) -> list[Field]:
        return [f for f in self.fields.values() if f.status in OPEN_STATUSES]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "subject": self.subject.to_dict(),
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "signals": [s.to_dict() for s in self.signals],
            "rules": [r.to_dict() for r in self.rules],
            "sources": [s.to_dict() for s in self.sources],
            "manual_steps": self.manual_steps,
            "gaps": [f.to_dict() for f in self.gaps()],
            "notes": self.notes,
            "evidence_count": sum(len(f.evidence) for f in self.fields.values())
            + sum(len(r.evidence) for r in self.rules),
        }
