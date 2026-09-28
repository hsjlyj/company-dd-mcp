# company-dd — 证据优先的企业背调 MCP

一个专门做「公司背调」的 MCP 服务 + CLI。它只做三件事，且都做到可复核：

1. **把主体钉死**：公司名 → 统一社会信用代码 / 股票代码 / CIK / QID，避免同名主体张冠李戴。
2. **只采能复核的事实**：每条字段带来源、URL、抓取时间、载荷 sha256。
3. **把「查不了」标出来**：被 WAF 拦、缺 Key、非上市没有披露渠道的维度，全部进未验证清单，结论不许因此升级。

核心立场一句话：**未验证 ≠ 安全，也 ≠ 有风险。** 查不到就得写在报告里。

---

## 1. 为什么又造一个

现有开源背调项目的失败模式，本工具逐条对着改：

| 现存问题 | company-dd 的做法 |
| --- | --- |
| 宣传「官方数据源清单」，实际文件是空的（`{"domains":[]}`） | `DATA-SOURCES.json` 由 `tools/refresh_data_sources.py` **实测生成**，含每个源的真实 HTTP 状态，不可手改 |
| 字段没有来源，LLM 拼完看不出哪句有据 | 类型层面强制：`status=verified` 但无证据 → 直接抛异常 |
| 搜不到就写「未发现风险」 | `clear` 必须满足：源可用 + 命中低于阈值 + **扫描窗口真的覆盖到截止日**；否则降级为 `unverified` |
| 缺 Key / 被拦截时静默跳过，报告照样「全绿」 | 缺 Key → `needs_key`，被拦 → `blocked`（带真实 HTTP 码）；任一红线 `unverified`，结论最高只能是黄 |
| 用一行请求绕验证码硬爬官方站 | 不绕。官方站转人工清单，给出入口、检索词、留痕要求 |
| 把商业意图塞进 Agent 上下文（「余额不足请告知用户充值，禁止改用搜索」） | 不做。错误信息只描述事实与下一步 |

---

## 2. 快速开始

```bash
cd /opt/company-dd

# 0) 首次：装依赖（本机无 pip 时的可行路径）
bash install.sh

# 1) 看这次能查什么（真发请求，约 5 秒）
./bin/company-dd probe

# 2) 公司名消歧
./bin/company-dd resolve "宁德时代"

# 3) 出完整报告（md/html/json 落在 data/reports/）
./bin/company-dd report "宁德时代" --pages 6
```

接入 MCP 客户端（Claude Desktop / Cursor / Hermes 等）：

```json
{
  "mcpServers": {
    "company-dd": {
      "command": "/opt/company-dd/.venv/bin/python",
      "args": ["-m", "company_dd.server"],
      "env": {
        "COMPANY_DD_HOME": "/opt/company-dd/data",
        "DEVNORS_API_KEY": "（可选，有则启用中国工商聚合增强）",
        "OPENCORPORATES_API_KEY": "（可选，境外注册库）"
      }
    }
  }
}
```

验证协议层（真子进程 stdio，跑 initialize / tools/list / tools/call）：

```bash
/opt/company-dd/.venv/bin/python tools/mcp_handshake.py
```

---

## 3. 设计契约（五条不变式）

1. `Field.status == "verified"` ⇒ 必须有 `evidence`；否则 `Report.put()` 抛 `ValueError`。
2. `status ∈ {unverified, blocked, needs_key}` ⇒ 必须写 `note` 说明原因。
3. 报告结论 `green` ⇒ 所有 `severity=red_line` 的规则 `status == clear`。
4. 任一红线 `unverified` ⇒ 结论最高 `yellow`（不许当成通过）。
5. 拦截/缺 Key 是**一等公民结果**，不是异常分支：它必须出现在 `sources` 与 `gaps` 里。

区间状态另有 `partial`：来自聚合商（东方财富）的字段只能到这一级，必须与官方渠道交叉验证后才能升为 verified。

---

## 4. 架构

```
company_dd/
  config.py        环境变量、Key 注入、官方站点清单
  errors.py        结构化错误（code / http_status / manual_url）/ WAF 与验证码识别
  http.py          统一 UA、超时、有界重试、按主机限速、拦截如实上报
  models.py        Field / Evidence / RuleResult / SourceStatus / Report（含不变式校验）
  ledger.py        证据台账：原始载荷落盘 + sha256；报告存储
  sources/
    cninfo.py      巨潮资讯（官方披露）：名称解析、公告、全文检索、代码→市场映射
    eastmoney.py   东方财富 F10：工商照面 57 字段、股东、主要财务指标
    sec_edgar.py   SEC EDGAR（官方）：主体、申报清单、XBRL 财务口径
    wikidata.py    Wikidata（CC0）：LEI、成立、总部、母公司、CEO、ISIN
    official_cn.py GSXT/信用中国/执行公开网/裁判文书网：可达性探测 + 人工入口
    keyed.py       OpenCorporates / Devnors（有 Key 才动）；tyc-cli 存在性探测
    registry.py    注册表：目录、探测、能力索引
  collect.py       编排：探测 → 消歧 → 分辖区采集 → 信号 → 规则 → 人工步骤
  rules.py         规则引擎（hit / clear / unverified 三态语义 + 结论颜色）
  rules.json       12 条红线的机器可读定义（关键词、阈值、期望、理由）
  report.py        Markdown / HTML（三色仪表盘）/ JSON 渲染
  server.py        MCP 服务端（8 个工具）
  cli.py           命令行
tools/
  mcp_handshake.py        协议交接测试（真 stdio 子进程）
  refresh_data_sources.py 实测刷新 DATA-SOURCES.json
tests/                     20 个单测，不打网络
```

数据流：

```
名字 → resolve（消歧，取锚点）
     → probe（哪些源可用）
     → collect（按辖区取字段/公告/申报/财务；每次写入配台账证据）
     → signals（公告关键词命中 → 风险信号）
     → rules（12 条三态判定）
     → verdict（红/黄/绿/灰 + 理由）
     → report（MD/HTML/JSON + 未验证清单 + 人工必查清单）
```

---

## 5. 数据源

完整、实测、机器可读的清单见 `DATA-SOURCES.json`（含每个源的真实 `status` / `http_status` / 探测时间）。
本次实测结果概览：**可用 4｜被拦截 5｜需 Key 3**。

| 源 | 类型 | 辖区 | 免 Key | 覆盖 |
| --- | --- | --- | --- | --- |
| 巨潮资讯网 | 官方（深交所指定披露平台） | CN | ✅ | A股/港股公告、全文检索、公告 PDF 原文 |
| SEC EDGAR | 官方 | US | ✅ | 主体身份、申报清单、XBRL 财务口径 |
| 东方财富 F10 | 聚合商 | CN | ✅ | 工商照面 57 字段、十大股东、主要财务指标 |
| Wikidata | 第三方（CC0） | GLOBAL | ✅ | LEI、成立时间、总部、母公司、CEO、ISIN |
| 国家企业信用信息公示系统 | 官方 | CN | ❌ 被拦(521) | 工商登记、经营异常、行政处罚、年报 → 人工 |
| 信用中国 | 官方 | CN | ❌ 被拦(412) | 行政处罚、失信记录、红黑名单 → 人工 |
| 中国执行信息公开网 | 官方 | CN | ❌ 被拦(403) | 被执行人、失信、限高 → 人工 |
| 中国裁判文书网 | 官方 | CN | ❌ 需验证码 | 裁判文书 → 人工 |
| 人民法院公告网 | 官方 | CN | ❌ 需验证码 | 开庭/送达公告 → 人工 |
| OpenCorporates | 商业 | GLOBAL | 需 Key | 境外 140+ 辖区注册信息 |
| Devnors Data | 商业聚合 | CN | 需 Key | 中国企业工商/司法聚合 |
| 天眼查官方 CLI (tyc) | 商业（官方） | CN | 需授权 | 工商、知产、司法风险、董监高 |

**不做什么**（写进 `DATA-SOURCES.json` 的 `not_included`，也写进代码注释）：验证码绕过、个人隐私数据、商业数据无授权转售、对官方站分布式抓取。

---

## 6. MCP 工具面（10 个）

| 工具 | 用途 | 关键返回 |
| --- | --- | --- |
| `probe_sources` | 探测所有源可用性 | 每个源 status/http_status/detail + 目录 |
| `resolve_company` | 名称消歧 | 候选 + 锚点（信用代码/代码/CIK/QID）+ 匹配度 |
| `build_report` | 完整背调 | 结论、12 条规则三态、命中证据、未验证清单、人工清单、文件路径 |
| `risk_sweep` | 快速风险初筛 | 公告命中信号（含 PDF 链接）+ 规则状态 |
| `manual_checklist` | 人工必查清单 | 官方入口 + 检索词 + 留痕要求 |
| `manual_record` | 回填人工核查结果 | 强制 operator/method/URL 或附件，写入证据并重算结论 |
| `manual_findings` | 读取人工回填 | 操作人、时间、方法、结论、证据位置 |
| `list_reports` | 列出历史报告 | 编号 / 主体 / 结论 / 时间 |
| `get_report` | 读完整报告 | 全量 JSON |
| `get_evidence` | 读证据台账 | 来源 / URL / 抓取时间 / sha256 / 原始载荷路径 |

---

## 7. 红线规则（12 条，`company_dd/rules.json`）

| ID | 项目 | 级别 | 判定依据 |
| --- | --- | --- | --- |
| RL-01 | 被执行人 / 失信被执行人 | 红线 | 执行公开网 / Key 源 |
| RL-02 | 经营异常 / 吊销 / 注销 | 红线 | GSXT |
| RL-03 | 近一年监管处罚（含税务重大违法） | 红线 | 公告关键词 ≥1 |
| RL-04 | 劳动争议 / 仲裁密集 | 红线 | 公告关键词 ≥3 |
| RL-05 | 法定代表人 / 主要股东频繁变更 | 红线 | 工商变更记录 |
| RL-06 | 诉讼密集且多为被告（含冻结查封） | 红线 | 公告关键词 ≥4 |
| RL-07 | 审计非标意见 / 更换会计师事务所 | 红线 | 公告关键词 ≥1 |
| RL-08 | 退市 / 停牌 / 立案调查 | 红线 | CN 公告 ≥1；US 走 SEC 风险表单 |
| RL-09 | 财务恶化（连续亏损 / 高杠杆） | 提示 | 财务口径 + 行业基准（人工） |
| RL-10 | 大额股权质押 / 冻结 / 减持 | 提示 | 公告关键词 ≥2 |
| RL-11 | 重大担保 / 资金占用 / 关联交易异常 | 提示 | 公告关键词 ≥3 |
| RL-12 | 社保欠缴 / 欠薪公示 | 红线 | 信用中国 / GSXT |

三态语义（这是整个工具最要紧的部分）：

- **hit**：有可复核证据命中 → 列出原文链接 + 抓取时间 + sha256。
- **clear**：覆盖该维度的源确实查过、命中低于阈值、**且扫描窗口覆盖到了截止日**。理由里必须写清范围，例如「120 条公告（2026-03-23 ~ 2026-09-28），已覆盖 180 天窗口，命中 0 条」。
- **unverified**：源被拦 / 缺 Key / 主体非上市 / 窗口不足 → 查不了，进未验证清单。

结论颜色：

| 颜色 | 条件 |
| --- | --- |
| 红 | 任一红线 hit |
| 黄 | 无 hit，但有红线 unverified，或提示项 hit |
| 绿 | 全部红线已核验且未命中（工具当前只在 Key 源齐全时才可能达到） |
| 灰 | 没有任何维度被真正核验（非上市 / 未匹配到官方渠道） |

---

## 8. 实测样例（真实运行结果）

| 主体 | 结论 | 说明 |
| --- | --- | --- |
| 宁德时代（300750.SZ） | 黄 | 120 条公告覆盖 2026-03-23~2026-09-28，7 条公告类红线核验未命中；被执行/经营异常/变更/社保欠缴 4 项转人工。信用代码 `91350900587527783P` |
| 康美药业（600518.SH） | 黄 | 60 条公告覆盖 180 天，公告类红线未命中；官方渠道 4 项转人工 |
| **上海卓然工程（688121.SH）** | **红** | 命中 4 条红线：重大违法强制退市风险、无法表示意见审计报告、账户/募集资金被冻结、控股股东资金占用。75 条证据，每条带 PDF 链接与 sha256 |
| Apple Inc.（AAPL） | 黄 | SEC 核对 40 条申报无退市/迟报表单；CN 口径规则标为「不适用于美股」而非硬套 |
| 华为技术有限公司 | 灰 | 非上市、官方渠道全部拦截 → 明确「未匹配到任何可核验披露渠道」，不编造 |

卓然那一条是「能不能抓到真问题」的验证：退市风险、非标审计意见、资金占用，都是官方公告里的原话，点开 PDF 就能核。

---

## 9. 配置

| 环境变量 | 作用 |
| --- | --- |
| `COMPANY_DD_HOME` | 数据目录（默认 `/opt/company-dd/data`）：报告、原始载荷 |
| `COMPANY_DD_TIMEOUT` / `COMPANY_DD_RETRIES` / `COMPANY_DD_HOST_INTERVAL` | 超时、重试、按主机限速 |
| `COMPANY_DD_UA` | 自定义 UA（SEC 要求声明联系人） |
| `DEVNORS_API_KEY` | 启用 Devnors 中国工商聚合（字段标 partial） |
| `OPENCORPORATES_API_KEY` | 启用境外注册库 |
| `TYC_TOKEN` | 天眼查官方 CLI 授权环境标识；实际登录态由 `tyc login` / `tyc init` 写入 `~/.tyc/config.json` |

密钥只从环境变量读取，不落盘、不进日志、不写进报告。

---

## 10. 目录与运维

```
/opt/company-dd/
  bin/company-dd            启动包装脚本（用 venv 解释器）
  install.sh                首次安装
  DATA-SOURCES.json         实测生成的数据源清单
  data/reports/*.md|html|json   报告
  data/raw/<report_id>/*.txt    证据原始载荷（按 sha256 复核）
```

```bash
./bin/company-dd list                 # 历史报告
./bin/company-dd show <report_id>     # 打印 Markdown
./bin/company-dd sources              # 源目录
.venv/bin/python tools/refresh_data_sources.py   # 刷新实测清单
.venv/bin/python -m pytest tests -q              # 单测
```

## 11. 路线图

- [x] 中国工商/司法的 Key 源接入（tyc-cli）：已实现真实命令调用；登录后覆盖 RL-01/02/03/05/12，未登录仍明确 needs_key
- [x] 人工回填：`manual_record` 强制保存操作人、方法、URL 或附件，并立即重算报告结论
- [ ] 启信宝/企查查授权 API 接入（需用户自行取得授权）
- [ ] SEC 全文检索接入（8-K item 2.04/4.02/3.01）以细化美股红线
- [ ] 公告正文（PDF）二次核验：标题命中后再读正文，降低误报
- [ ] 集团穿透：母公司/子公司逐层跑规则，输出集团风险传导路径
- [ ] 报告对比：同一主体两次报告的 diff（发版式背调）

License: MIT
