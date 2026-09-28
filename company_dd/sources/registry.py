"""数据源注册表：把「有哪些源、各自什么状态、覆盖什么」变成可查询的对象。"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..http import HttpClient
from ..models import SourceStatus
from .base import Source
from .cninfo import CninfoSource
from .eastmoney import EastmoneySource
from .keyed import DevnorsSource, OpenCorporatesSource
from .official_cn import OfficialCnSource
from .sec_edgar import SecEdgarSource
from .tyc_cli import TycCliSource
from .wikidata import WikidataSource


class Registry:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.http = http or HttpClient(config)
        self.sources: dict[str, Source] = {}
        for cls in (
            CninfoSource,
            EastmoneySource,
            SecEdgarSource,
            WikidataSource,
            OfficialCnSource,
            OpenCorporatesSource,
            DevnorsSource,
            TycCliSource,
        ):
            src = cls(config, self.http)
            self.sources[src.id] = src

    def get(self, source_id: str) -> Source:
        return self.sources[source_id]

    def catalog(self) -> list[dict[str, Any]]:
        return [s.spec.to_dict() for s in self.sources.values()]

    def probes(self) -> list[SourceStatus]:
        out: list[SourceStatus] = []
        for src in self.sources.values():
            if isinstance(src, OfficialCnSource):
                out.extend(src.probe_all())
            else:
                out.append(src.probe())
        return out

    def status_index(self, statuses: list[SourceStatus]) -> dict[str, SourceStatus]:
        return {s.source_id: s for s in statuses}

    def available(self, capability: str, statuses: list[SourceStatus]) -> list[str]:
        idx = self.status_index(statuses)
        return [
            s.id
            for s in self.sources.values()
            if capability in s.spec.capabilities and idx.get(s.id, None) is not None
            and idx[s.id].status == "ok"
        ]
