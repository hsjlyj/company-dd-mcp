#!/usr/bin/env python3
"""刷新 DATA-SOURCES.json：把「有哪些源、实测什么状态、覆盖什么、不覆盖什么」写死进仓库。

与那些把数据源清单留成空文件的项目的区别：这个文件由实测生成，不可手改。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from company_dd import __version__  # noqa: E402
from company_dd.collect import Engine  # noqa: E402
from company_dd.config import Config  # noqa: E402
from company_dd.models import now_iso  # noqa: E402
from company_dd.sources.official_cn import MANUAL_LINKS, SEARCH_HINT  # noqa: E402

DISCLOSURE = (
    "本文件由 tools/refresh_data_sources.py 实测生成：每个源的 status/http_status 都来自一次真实请求。"
    "状态 ok 只代表本次探测可用，不代表字段完整。需要 Key 的源在未配置时如实标 needs_key，不静默省略。"
)

NOT_INCLUDED = [
    {
        "what": "验证码 / 滑块 / 登录墙绕过",
        "why": "不做。被 WAF 拦住的官方站点转人工清单，工具只给入口和检索词。",
    },
    {
        "what": "个人隐私数据（手机号机主、身份证、住址、开房记录等）",
        "why": "不做。背调公司的边界是法人主体与公开司法信息，不碰自然人隐私数据。",
    },
    {
        "what": "商业数据源的无授权转售或爬取",
        "why": "不做。需要工商/司法深度字段时，走官方 CLI 或商业 API 并自带 Key。",
    },
    {
        "what": "对官方站点的分布式抓取",
        "why": "不做。对巨潮/SEC 等允许程序化访问的源按主机限速，其余一律转人工。",
    },
]


def main() -> int:
    cfg = Config()
    engine = Engine(cfg)
    statuses = engine.registry.probes()
    idx = {s.source_id: s for s in statuses}
    sources = []
    for spec in engine.registry.catalog():
        st = idx.get(spec["id"])
        entry = {
            **spec,
            "access": "open" if not spec.get("requires_key") else "key_required",
            "probe": {
                "status": st.status if st else "unknown",
                "http_status": st.http_status if st else None,
                "url": st.url if st else spec["homepage"],
                "detail": st.detail if st else "",
                "checked_at": st.fetched_at if st else now_iso(),
            },
        }
        sources.append(entry)

    # 官方渠道逐站点状态（official_cn 展开成独立条目）
    for st in statuses:
        if st.source_id in MANUAL_LINKS:
            sources.append(
                {
                    "id": st.source_id,
                    "label": st.label,
                    "kind": "official",
                    "jurisdiction": "CN",
                    "homepage": MANUAL_LINKS[st.source_id],
                    "capabilities": ["manual_route"],
                    "requires_key": "",
                    "access": "human_only",
                    "note": SEARCH_HINT.get(st.source_id, ""),
                    "probe": {
                        "status": st.status,
                        "http_status": st.http_status,
                        "url": st.url,
                        "detail": st.detail,
                        "checked_at": st.fetched_at,
                    },
                }
            )

    doc = {
        "product": "company-dd",
        "version": __version__,
        "updated": now_iso(),
        "disclosure_policy": DISCLOSURE,
        "summary": {
            "total": len(sources),
            "ok": sum(1 for s in sources if s["probe"]["status"] == "ok"),
            "blocked": sum(1 for s in sources if s["probe"]["status"] == "blocked"),
            "needs_key": sum(1 for s in sources if s["probe"]["status"] == "needs_key"),
        },
        "sources": sources,
        "not_included": NOT_INCLUDED,
        "evidence_policy": {
            "field_status": ["verified", "partial", "unverified", "blocked", "needs_key", "not_applicable"],
            "rule": "verified 字段必须带证据（来源+URL+抓取时间+sha256）；其余状态必须写明原因。",
            "green_rule": "只有全部红线维度 status=clear 才可能判绿；任一红线 unverified，结论最高为黄。",
        },
    }
    out = ROOT / "DATA-SOURCES.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes) —— {doc['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
