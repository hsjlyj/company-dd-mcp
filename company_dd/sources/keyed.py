"""需要商业数据源 Key 的渠道 —— 缺 Key 时如实标记 needs_key，不静默跳过。

已实现（有公开文档的接口）：
  * OpenCorporates  —— 全球 140+ 辖区公司注册检索（需 key）
  * Devnors Data    —— 中国企业工商/司法聚合（需 key）
天眼查 CLI 有独立适配器，只有检测到真实登录态才执行已核实的命令。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import DdError
from ..models import SourceStatus, now_iso
from .base import Source, SourceSpec

OC_API = "https://api.opencorporates.com/v0.4/companies/search"
DEVNORS = "https://data.devnors.com/v1/data/query"


class OpenCorporatesSource(Source):
    spec = SourceSpec(
        id="opencorporates",
        label="OpenCorporates（全球公司注册库）",
        kind="keyed",
        jurisdiction="GLOBAL",
        homepage="https://opencorporates.com/",
        capabilities=["registry_lookup"],
        requires_key="OPENCORPORATES_API_KEY",
        note="免费公共接口常 401，配置 Key 后可用；覆盖境外主体。",
    )

    def probe(self) -> SourceStatus:  # type: ignore[override]
        if not self.key_present():
            return self.needs_key_status()
        try:
            data = self._search("apple", limit=1)
            return self.ok(OC_API, f"接口正常，命中 {data.get('total')} 条", 200)
        except Exception as exc:  # noqa: BLE001
            return self.failed(OC_API, exc)

    def _search(self, name: str, limit: int = 5) -> dict[str, Any]:
        key = self.config.keys.get(self.spec.id) or ""
        r = self.http.request(
            "GET", OC_API, source=self.id, label=self.spec.label,
            params={"q": name, "api_token": key, "per_page": limit},
        )
        data = r.json()
        results = ((data.get("results") or {}).get("companies")) or []
        return {
            "total": (data.get("results") or {}).get("total_count"),
            "items": [
                {
                    "name": (x.get("company") or {}).get("name"),
                    "number": (x.get("company") or {}).get("company_number"),
                    "jurisdiction": (x.get("company") or {}).get("jurisdiction_code"),
                    "status": (x.get("company") or {}).get("current_status"),
                    "incorporation": (x.get("company") or {}).get("incorporation_date"),
                    "type": (x.get("company") or {}).get("company_type"),
                    "url": (x.get("company") or {}).get("opencorporates_url"),
                }
                for x in results
            ],
            "raw": data,
        }

    def search(self, name: str, limit: int = 5) -> dict[str, Any]:
        if not self.key_present():
            st = self.needs_key_status()
            raise DdError(
                st.detail, code="needs_key", source=self.id, url=self.spec.homepage,
                hint="配置 OPENCORPORATES_API_KEY 后重试。",
            )
        return self._search(name, limit)


class DevnorsSource(Source):
    spec = SourceSpec(
        id="devnors",
        label="Devnors Data（中国企业数据聚合，需 Key）",
        kind="keyed",
        jurisdiction="CN",
        homepage="https://data.devnors.com/",
        capabilities=["cn_registry", "cn_judicial"],
        requires_key="DEVNORS_API_KEY",
        note="第三方聚合，数据授权口径未公开披露；字段默认 partial，需交叉验证。",
    )

    def probe(self) -> SourceStatus:  # type: ignore[override]
        if not self.key_present():
            return self.needs_key_status()
        try:
            data = self.query("enterprise", "company_detail_v2", "宁德时代新能源科技股份有限公司")
            return self.ok(DEVNORS, f"接口正常，命中 {data.get('total_hits')} 条", 200)
        except Exception as exc:  # noqa: BLE001
            return self.failed(DEVNORS, exc)

    def query(self, domain: str, type_: str, query: str, top_k: int = 5,
              filters: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.key_present():
            raise DdError(
                self.needs_key_status().detail, code="needs_key", source=self.id,
                url=self.spec.homepage, hint="配置 DEVNORS_API_KEY 后重试。",
            )
        key = self.config.keys.get(self.spec.id) or ""
        body: dict[str, Any] = {"domain": domain, "type": type_, "query": query, "top_k": top_k}
        if filters:
            body["filters"] = filters
        r = self.http.request(
            "POST", DEVNORS, source=self.id, label=self.spec.label, json_body=body,
            headers={"Authorization": f"Bearer {key}"},
        )
        data = r.json()
        return {
            "total_hits": data.get("total_hits"),
            "units": data.get("units"),
            "request_id": data.get("request_id"),
            "hits": data.get("hits") or [],
            "raw": data,
        }


class LocalCliSource(Source):
    """探测本机是否装了天眼查官方 CLI / 企查查 MCP，不代跑未知子命令。"""

    spec = SourceSpec(
        id="tianyancha_cli",
        label="天眼查官方 CLI（tyc，本机可选）",
        kind="keyed",
        jurisdiction="CN",
        homepage="https://github.com/tyc-tech/tyc-cli",
        capabilities=["cn_registry", "cn_judicial"],
        requires_key="TYC_TOKEN",
        note="数据权威（天眼查官方），但需商务授权；本工具只探测是否可用并给出接入建议。",
    )

    def probe(self) -> SourceStatus:  # type: ignore[override]
        binary = shutil.which("tyc")
        token_file = Path.home() / ".tyc" / "config.json"
        if not binary:
            return SourceStatus(
                source_id=self.id, label=self.spec.label, kind=self.spec.kind,
                jurisdiction="CN", status="needs_key", url=self.spec.homepage,
                detail="本机未安装 tyc CLI；已登录的官方 CLI 可覆盖工商与司法维度（需商务授权）。",
                fetched_at=now_iso(), manual_url=self.spec.homepage,
            )
        version = ""
        try:
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip()[:80]
        except Exception:  # noqa: BLE001
            pass
        if token_file.exists():
            return self.ok(self.spec.homepage, f"已安装 {binary} {version}，且存在登录态 {token_file}")
        return SourceStatus(
            source_id=self.id, label=self.spec.label, kind=self.spec.kind, jurisdiction="CN",
            status="needs_key", url=self.spec.homepage,
            detail=f"已安装 {binary} {version}，但未见登录态 {token_file}；先执行 tyc login。",
            fetched_at=now_iso(), manual_url=self.spec.homepage,
        )
