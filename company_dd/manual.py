"""人工核查回填 —— 把「人在官方系统里查到的东西」变成一等证据。

为什么必须有：GSXT / 信用中国 / 执行公开网 / 裁判文书网 实测对自动化流量
全拦（521/412/403/验证码）。要么接商业 Key，要么人工过一遍。
人工结果不能只是「我记得没事」，必须留下：谁、什么时候、用什么方法、
在哪个 URL 看到什么，以及附件路径。

规则语义：人工结论与机器结论同等对待——
  outcome=clear → 该红线 clear（理由里注明是人工核查）
  outcome=hit   → 该红线 hit（带人工证据）
不留痕的回填一律拒绝（缺 operator/method/url-or-attachment 直接抛错）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import DdError
from .models import Evidence, RuleResult, now_iso

MANUAL_DIR = "manual"


@dataclass
class ManualFinding:
    rule_id: str
    outcome: str            # clear | hit
    operator: str
    method: str
    url: str = ""
    attachment: str = ""
    note: str = ""
    checked_at: str = field(default_factory=now_iso)
    finding_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_evidence(self) -> Evidence:
        attachment = Path(self.attachment).expanduser() if self.attachment else None
        digest = ""
        raw_path = ""
        if attachment:
            raw_path = str(attachment.resolve())
            h = hashlib.sha256()
            with attachment.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        return Evidence(
            source_id="manual",
            source_label=f"人工核查（{self.operator}）",
            url=self.url or self.attachment or "about:manual",
            fetched_at=self.checked_at,
            pointer=f"{self.rule_id} / {self.method}",
            sha256=digest,
            raw_path=raw_path,
            quote=self.note or self.method,
        )


class ManualStore:
    def __init__(self, home: Path) -> None:
        self.dir = Path(home) / MANUAL_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, report_id: str) -> Path:
        return self.dir / f"{report_id}.json"

    def add(self, report_id: str, finding: ManualFinding) -> Path:
        if finding.outcome not in ("clear", "hit"):
            raise DdError("outcome 只能是 clear 或 hit", code="invalid_outcome", source="manual")
        if not finding.operator.strip():
            raise DdError("必须记录核查人（operator）", code="missing_operator", source="manual",
                          hint="无主体的结论不可用于报告。")
        if not finding.method.strip():
            raise DdError("必须记录核查方法（method），例如「GSXT 手动检索」", code="missing_method",
                          source="manual")
        if not (finding.url.strip() or finding.attachment.strip()):
            raise DdError(
                "必须给出 URL 或附件路径，否则无法复核",
                code="missing_trace", source="manual",
                hint="截图/PDF 存本地并把路径填进 attachment。",
            )
        if finding.attachment:
            attachment = Path(finding.attachment).expanduser()
            if not attachment.is_file():
                raise DdError(
                    f"附件不存在或不是文件：{attachment}",
                    code="missing_attachment", source="manual",
                    hint="先把截图/PDF 保存到本机，再提交绝对路径。",
                )
        rows = self.load(report_id)
        rows = [r for r in rows if r.get("rule_id") != finding.rule_id]
        rows.append(finding.to_dict())
        self.path(report_id).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path(report_id)

    def load(self, report_id: str) -> list[dict[str, Any]]:
        p = self.path(report_id)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []

    def index(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for p in self.dir.glob("*.json"):
            out[p.stem] = {r["rule_id"]: r for r in self.load(p.stem)}
        return out


def rules_from_dict(rows: list[dict[str, Any]]) -> list[RuleResult]:
    out = []
    for r in rows or []:
        out.append(
            RuleResult(
                rule_id=r["rule_id"],
                title=r["title"],
                severity=r["severity"],
                status=r["status"],
                reason=r.get("reason", ""),
                evidence=[Evidence(**e) for e in (r.get("evidence") or [])],
                manual_url=r.get("manual_url", ""),
            )
        )
    return out


def reevaluate_report(data: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    """把人工结论合并进已保存的报告并重算结论颜色（供 record 命令/MCP 工具用）。"""
    from .rules import verdict_of

    rules = apply_manual(rules_from_dict(data.get("rules") or []), findings)
    verdict, reason = verdict_of(
        rules,
        jurisdiction=(data.get("subject") or {}).get("jurisdiction", "unknown"),
        listing=(data.get("subject") or {}).get("listing", ""),
    )
    data["rules"] = [r.to_dict() for r in rules]
    data["verdict"] = verdict
    data["verdict_reason"] = reason
    return data


def apply_manual(rules: list[RuleResult], findings: list[dict[str, Any]]) -> list[RuleResult]:
    """把人工结论合并进规则结果。人工优先于机器未验证。"""
    by_rule = {f["rule_id"]: f for f in findings}
    for r in rules:
        f = by_rule.get(r.rule_id)
        if not f:
            continue
        ev = ManualFinding(**{k: v for k, v in f.items() if k in ManualFinding.__annotations__})
        r.status = "clear" if f["outcome"] == "clear" else "hit"
        r.reason = (
            f"人工核查：{f['operator']} 于 {f['checked_at']} 用「{f['method']}」核查，"
            f"结论 {f['outcome']}。{f.get('note') or ''}"
        ).strip()
        r.evidence = [ev.to_evidence()]
    return rules
