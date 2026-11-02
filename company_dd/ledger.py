"""证据台账：每条证据落盘存原始载荷 + 哈希，报告里只引用台账编号。

为什么落盘：报告要能被人复核。别人拿到 URL 时站点可能已改版，
原始载荷 + sha256 是「我确实在某时刻看到过这段内容」的唯一凭据。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import Evidence, Report, now_iso

REPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_report_id(report_id: str) -> str:
    """报告 ID 只允许安全文件名字符，避免 MCP/CLI 输入造成路径穿越。"""
    value = (report_id or "").strip()
    if not REPORT_ID_RE.fullmatch(value):
        raise ValueError(f"非法报告编号：{report_id!r}")
    return value


class Ledger:
    def __init__(self, home: Path, report_id: str) -> None:
        self.dir = Path(home) / "raw" / validate_report_id(report_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.items: list[Evidence] = []

    def add(
        self,
        *,
        source_id: str,
        source_label: str,
        url: str,
        payload: str,
        pointer: str = "",
        quote: str = "",
    ) -> Evidence:
        blob = (payload or "").encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()
        idx = len(self.items) + 1
        safe_source = re.sub(r"[^A-Za-z0-9_-]+", "-", source_id).strip("-") or "source"
        path = self.dir / f"{idx:03d}-{safe_source}-{digest[:8]}.txt"
        if not path.exists():
            path.write_bytes(blob)
        ev = Evidence(
            source_id=source_id,
            source_label=source_label,
            url=url,
            fetched_at=now_iso(),
            pointer=pointer,
            sha256=digest,
            raw_path=str(path),
            quote=(quote or "")[:600],
        )
        self.items.append(ev)
        return ev

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.items], ensure_ascii=False, indent=2)


class ReportStore:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.dir = self.home / "reports"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, report: Report) -> Path:
        path = self.dir / f"{validate_report_id(report.report_id)}.json"
        path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def load(self, report_id: str) -> dict[str, Any]:
        safe_id = validate_report_id(report_id)
        path = self.dir / f"{safe_id}.json"
        if not path.exists():
            matches = sorted(self.dir.glob(f"{safe_id}*.json"))
            if not matches:
                raise FileNotFoundError(f"报告不存在：{safe_id}")
            path = matches[0]
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            rows.append(
                {
                    "report_id": d.get("report_id"),
                    "name": (d.get("subject") or {}).get("name") or (d.get("subject") or {}).get("query"),
                    "verdict": d.get("verdict"),
                    "generated_at": d.get("generated_at"),
                    "path": str(p),
                }
            )
        return rows
