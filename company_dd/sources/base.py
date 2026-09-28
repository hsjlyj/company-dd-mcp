"""数据源基类。

约定：
  * 一个 Source 只负责一个渠道，解析失败就抛错，不猜、不补。
  * probe() 必须真的发一次请求（或至少一次 TCP/TLS 探测），返回实测状态。
  * 需要 Key 的源在缺 Key 时 status=needs_key，而不是静默跳过。
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config
from ..errors import DdError
from ..http import HttpClient
from ..models import SourceStatus, now_iso


@dataclass
class SourceSpec:
    id: str
    label: str
    kind: str               # official | aggregator | third_party | keyed | manual
    jurisdiction: str       # CN | US | GLOBAL | HK
    homepage: str
    capabilities: list[str] = field(default_factory=list)
    requires_key: str = ""  # 环境变量名
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Source(abc.ABC):
    spec: SourceSpec

    def __init__(self, config: Config, http: HttpClient) -> None:
        self.config = config
        self.http = http

    @property
    def id(self) -> str:
        return self.spec.id

    def key_present(self) -> bool:
        return not self.spec.requires_key or bool(
            self.config.keys.get(self.spec.id) or __import__("os").getenv(self.spec.requires_key, "")
        )

    def needs_key_status(self) -> SourceStatus:
        return SourceStatus(
            source_id=self.spec.id,
            label=self.spec.label,
            kind=self.spec.kind,
            jurisdiction=self.spec.jurisdiction,
            status="needs_key",
            url=self.spec.homepage,
            detail=f"需要环境变量 {self.spec.requires_key}；未配置时该维度不采集。",
            fetched_at=now_iso(),
        )

    @abc.abstractmethod
    def probe(self) -> SourceStatus:
        """实测一次，返回真实可用性。"""

    def ok(self, url: str, detail: str = "", http_status: int | None = None) -> SourceStatus:
        return SourceStatus(
            source_id=self.spec.id,
            label=self.spec.label,
            kind=self.spec.kind,
            jurisdiction=self.spec.jurisdiction,
            status="ok",
            http_status=http_status,
            url=url,
            detail=detail,
            fetched_at=now_iso(),
        )

    def failed(self, url: str, exc: Exception) -> SourceStatus:
        if isinstance(exc, DdError):
            return SourceStatus(
                source_id=self.spec.id,
                label=self.spec.label,
                kind=self.spec.kind,
                jurisdiction=self.spec.jurisdiction,
                status="blocked" if exc.code == "blocked" else "error",
                http_status=exc.http_status,
                url=exc.url or url,
                detail=f"{exc.message} {exc.hint}".strip(),
                fetched_at=now_iso(),
                manual_url=exc.manual_url,
            )
        return SourceStatus(
            source_id=self.spec.id,
            label=self.spec.label,
            kind=self.spec.kind,
            jurisdiction=self.spec.jurisdiction,
            status="error",
            url=url,
            detail=str(exc)[:300],
            fetched_at=now_iso(),
        )
