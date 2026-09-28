"""company-dd — 企业背调 MCP：证据优先、缺口显式、绝不编造。

对外只有一个产品契约：
  每个结论必须能指回一条证据（来源 + URL + 抓取时间 + 载荷哈希）；
  拿不到就标 blocked / needs_key / unverified，并进「未验证清单」——
  未验证不等于安全，也不等于有风险。
"""

__version__ = "0.1.0"

PRODUCT = "company-dd"
