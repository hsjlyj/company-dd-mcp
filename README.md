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
cd /workspace/company-dd-mcp

# 0) 首次：安装项目与依赖（本机无 pip 时的可行路径）
bash install.sh

# 1) 看这次能查什么（真发请求，约 5 秒）
./bin/company-dd probe

# 2) 公司名消歧
./bin/company-dd resolve "宁德时代"

# 3) 出完整报告（md/html/json 落在 data/reports/）
./bin/company-dd report "宁德时代" --pages 6
```

`install.sh` 会以 editable 方式安装本项目，因此 `./bin/company-dd` 和 venv 里的 `company-dd` 命令都可以在任意目录使用；安装脚本与 `bin/company-dd` 都按自身位置推导项目根目录，**不依赖固定安装路径**，放在任何目录都能直接跑。安装冒烟测试默认离线，不会改写 `DATA-SOURCES.json`。

接入 MCP 客户端（Claude Desktop / Cursor / DSH 等）：

```json
{
  "mcpServers": {
    "company-dd": {
      "command": "/workspace/company-dd-mcp/.venv/bin/python",
      "args": ["-m", "company_dd.server"],
      "env": {
        "COMPANY_DD_HOME": "/workspace/company-dd-mcp/data",
        "DEVNORS_API_KEY": "（可选，有则启用中国工商聚合增强）",
        "OPENCORPORATES_API_KEY": "（可选，境外注册库）"
      }
    }
  }
}
```

> 换目录部署时，把上面两处绝对路径换成你的项目位置即可；`bin/company-dd` 是更省事的入口，它内部已自动处理路径。

验证协议层（真子进程 stdio，跑 initialize / tools/list / tools/call）：

```bash
/workspace/company-dd-mcp/.venv/bin/python tools/mcp_handshake.py
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
  server.py        MCP 服务端（10 个工具）
  cli.py           命令行
tools/
  mcp_handshake.py        协议交接测试（真 stdio 子进程）
  refresh_data_sources.py 实测刷新 DATA-SOURCES.json（--offline 可离线冒烟）
tests/                     离线单测，不打网络
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
本次实测结果概览：**可用 4｜被拦截 5｜需 Key 3**（共 13 个维度）。

### 5.1 概览表

| 源 ID | 名称 | 类型 | 辖区 | 状态 | 覆盖 |
| --- | --- | --- | --- | --- | --- |
| `cninfo` | 巨潮资讯网 | 官方 | CN | ✅ ok | A股/港股公告、全文检索、PDF 原文 |
| `sec_edgar` | SEC EDGAR | 官方 | US | ✅ ok | 主体身份、申报清单、XBRL 财务口径 |
| `eastmoney` | 东方财富 F10 | 聚合商 | CN | ✅ ok | 工商照面 57 字段、十大股东、主要财务指标 |
| `wikidata` | Wikidata | 第三方（CC0） | GLOBAL | ✅ ok | LEI、成立时间、总部、母公司、CEO、ISIN |
| `official_cn` | 中国官方渠道打包 | 官方 | CN | ⚠️ 人工 | 公示系统/信用中国/执行公开网/裁判文书/公告网 |
| `opencorporates` | OpenCorporates | 商业 | GLOBAL | 🔑 needs_key | 境外 140+ 辖区公司注册信息 |
| `devnors` | Devnors Data | 商业聚合 | CN | 🔑 needs_key | 中国企业工商/司法聚合 |
| `tianyancha_cli` | 天眼查官方 CLI | 商业（官方） | CN | 🔑 needs_key | 工商、知产、司法风险、董监高、变更记录 |

> **官方渠道打包说明**：`official_cn` 是 5 个中国官方站点的集合（国家企业信用信息公示系统、信用中国、中国执行信息公开网、中国裁判文书网、人民法院公告网），它们全部被 WAF/验证码拦截，无法程序化采集，工具只会标记为 `blocked` 并在报告中给出人工入口和操作指引。

### 5.2 环境变量配置

需要 Key 的源通过环境变量注入，所有密钥**只从环境变量读取，不落盘、不进日志、不写进报告**。

| 环境变量 | 对应源 ID | 作用 | 获取方式 |
| --- | --- | --- | --- |
| `OPENCORPORATES_API_KEY` | `opencorporates` | 境外公司注册信息 | https://opencorporates.com/api_info |
| `DEVNORS_API_KEY` | `devnors` | 中国企业工商/司法聚合 | 联系 Devnors 商务 |
| `TYC_TOKEN` | `tianyancha_cli` | 天眼查 CLI 授权标识 | `tyc login` 交互登录后写入 `~/.tyc/config.json` |

**配置示例**（在 shell 或 `.env` 文件中）：

```bash
# 境外源
export OPENCORPORATES_API_KEY="your-key-here"

# 中国聚合源
export DEVNORS_API_KEY="your-key-here"

# 天眼查（推荐用 tyc login 交互登录，会自动写配置）
export TYC_TOKEN="your-token-here"
```

> **天眼查 CLI 的两种登录方式**：
> 1. `tyc login` — 交互式，浏览器扫码/账号密码，token 自动写入 `~/.tyc/config.json`
> 2. `tyc init --authorization <KEY>` — 直接写入 KEY
>
> 登录成功后，`tyc` 二进制会维护自己的 session。**`TYC_TOKEN` 环境变量是给本工具的备用路径**，当 CLI 未安装或未登录时尝试回退使用。

### 5.3 免 Key 公开源接入详解

#### 5.3.1 巨潮资讯网（`cninfo`）

- **是什么**：深交所指定的信息披露平台，A股/港股上市公司官方公告的唯一电子化出口
- **接入方式**：无需任何配置，直接可用
- **接口特征**：`https://www.cninfo.com.cn/new/information/topSearch/query`，POST 请求，JSON 响应
- **返回字段**：公告标题、公告时间、公告类型、PDF 下载链接、股票代码
- **覆盖范围**：A股主板/创业板/科创板/北交所、港股（H 股）上市公司公告
- **限制**：
  - 非上市主体无数据
  - PDF 原文需二次下载（工具会尝试获取）
  - 公告全文检索有频率限制
- **验证**：
  ```bash
  # 探测是否可用
  ./bin/company-dd probe
  # 输出中 cninfo 应显示 status=ok
  ```

#### 5.3.2 SEC EDGAR（`sec_edgar`）

- **是什么**：美国证监会官方电子数据收集、分析和检索系统
- **接入方式**：无需 Key，但**必须在请求头中声明 User-Agent**（工具已内置，格式为 `company-dd-mcp/{ver} (+https://localhost/company-dd; due-diligence research tool; respects robots and rate limits)`）
- **接口特征**：
  - 主体解析：`https://data.sec.gov/submissions/CIK{code}.json`
  - 申报清单：`https://data.sec.gov/submissions/CIK{code}.json` 中的 `filings` 字段
  - XBRL 财务数据：通过 accession number 构建 URL
- **返回字段**：CIK、ticker、公司名、SIC 代码、申报清单（含 accessionNumber, filingDate, reportDate, primaryDocDescription）
- **覆盖范围**：所有在美国 SEC 注册的主体（含外国私人发行人）
- **限制**：
  - 要求遵守爬虫协议（EDGAR 的 `robots.txt`）
  - 大批量请求需自行限速（工具默认 0.8 秒/主机）
  - XBRL 口径和财报解读需要专业知识
- **验证**：
  ```bash
  # 解析 Apple Inc.
  ./bin/company-dd resolve "Apple Inc."
  # 应返回 CIK 320193, ticker AAPL
  ```

#### 5.3.3 东方财富 F10（`eastmoney`）

- **是什么**：国内最大的金融数据聚合平台之一，F10 资料页涵盖公司基本面
- **接入方式**：无需 Key，直接请求
- **接口特征**：`https://datacenter-web.eastmoney.com/api/data/v1/get`，带加密参数和 cookies
- **返回字段**（工商照面）：57 个字段，包括统一社会信用代码、注册地址、法定代表人、注册资本、成立日期、经营范围、十大股东、主要财务指标等
- **覆盖范围**：A股/港股/美股上市公司
- **⚠️ 重要**：东方财富是**聚合商**，不是官方源。返回的字段标记为 `partial`，必须与官方渠道（巨潮公告）交叉验证后才能升级为 `verified`
- **验证**：
  ```bash
  ./bin/company-dd probe
  # 输出中 eastmoney 应显示 status=ok, detail="样例字段 57 个"
  ```

#### 5.3.4 Wikidata（`wikidata`）

- **是什么**：维基媒体基金会维护的开放知识库，CC0 协议
- **接入方式**：无需 Key，通过 SPARQL 查询
- **返回字段**：LEI（法人识别码）、成立日期、总部所在地、母公司、CEO、ISIN、行业分类
- **覆盖范围**：全球范围内的知名实体（上市公司、大型企业、组织）
- **限制**：
  - 数据由社区编辑，可能滞后
  - **不作工商凭证使用**，仅作辅助参考
  - 非上市公司覆盖稀疏
- **验证**：
  ```bash
  ./bin/company-dd resolve "Apple"
  # Wikidata 应出现在候选列表中
  ```

### 5.4 需 Key 商业源接入详解

#### 5.4.1 OpenCorporates（`opencorporates`）

- **是什么**：全球最大的公司注册信息开放数据库，覆盖 140+ 辖区
- **Key 获取**：https://opencorporates.com/api_info 免费注册获取
- **配置**：
  ```bash
  export OPENCORPORATES_API_KEY="your-key"
  ```
- **返回字段**：公司名称、注册号、注册地、成立日期、状态（active/inactive）、注册地址
- **适用场景**：境外主体身份核验、离岸公司调查
- **验证**：
  ```bash
  export OPENCORPORATES_API_KEY="your-key"
  ./bin/company-dd probe
  # opencorporates 应从 needs_key 变为 ok
  ```

#### 5.4.2 Devnors Data（`devnors`）

- **是什么**：中国企业工商/司法数据聚合 API
- **Key 获取**：联系 Devnors 商务获取
- **配置**：
  ```bash
  export DEVNORS_API_KEY="your-key"
  ```
- **返回字段**：工商登记信息、司法风险（被执行人/失信）、行政处罚
- **⚠️ 注意**：
  - 数据授权口径未公开披露，字段标记为 `partial`
  - 需要与官方渠道交叉验证
  - 启用后会覆盖 RL-01（被执行人/失信被执行人）维度的 `needs_key` 状态
- **验证**：
  ```bash
  export DEVNORS_API_KEY="your-key"
  ./bin/company-dd probe
  # devnors 应从 needs_key 变为 ok
  # 跑报告时 RL-01 应从 unverified 变为 partial 或 verified
  ```

#### 5.4.3 天眼查 CLI（`tianyancha_cli`）

- **是什么**：天眼查官方提供的命令行工具，覆盖工商登记、经营异常、行政处罚、被执行人/失信、变更记录等
- **接入方式**（推荐）：
  ```bash
  # 安装 tyc CLI（参考 tyc 官方文档）
  # 交互登录
  tyc login
  # 按提示完成验证，token 自动写入 ~/.tyc/config.json
  ```
- **替代方式**：
  ```bash
  export TYC_TOKEN="your-token-from-tyc-init"
  ```
- **覆盖的红线规则**：RL-01（被执行人/失信）、RL-02（经营异常/吊销）、RL-03（行政处罚）、RL-05（变更记录）
- **⚠️ 注意**：
  - 需要商务授权，仅限本机使用
  - 工具只检测本地 tyc CLI 是否安装并登录，**不代跑任何未知子命令**
  - 登录态文件在 `~/.tyc/config.json`，确认文件存在且未过期
- **验证**：
  ```bash
  # 先确认 tyc CLI 可用
  tyc --version
  tyc company info "宁德时代"  # 应返回数据
  
  # 然后跑 probe
  ./bin/company-dd probe
  # tianyancha_cli 应显示已安装版本号
  ```

### 5.5 被拦截官方站（人工核查）

以下 5 个中国官方站点全部被 WAF/验证码拦截，无法程序化采集。工具会在报告中标记为 `blocked`，并提供人工入口和操作指引。

#### 国家企业信用信息公示系统
- **URL**：https://www.gsxt.gov.cn/
- **HTTP 状态**：521（WAF 拦截）
- **覆盖**：工商登记、经营异常名录、行政处罚、年报
- **人工操作**：在首页搜索框输入企业全称 → 过滑块验证 → 核对「基础信息 / 经营异常 / 行政处罚 / 年报」各栏目
- **对应红线**：RL-02（经营异常/吊销/注销）、RL-12（社保欠缴）

#### 信用中国
- **URL**：https://www.creditchina.gov.cn/
- **HTTP 状态**：412（WAF 拦截）
- **覆盖**：行政处罚、失信记录、红黑名单
- **人工操作**：进入「信用服务」→ 按企业名称搜索 → 查看「行政处罚 / 失信记录 / 红黑名单」
- **对应红线**：RL-03（行政处罚）、RL-12（失信记录）

#### 中国执行信息公开网
- **URL**：https://zxgk.court.gov.cn/
- **HTTP 状态**：403（WAF 拦截）
- **覆盖**：被执行人、失信被执行人、限制消费
- **人工操作**：分别在「被执行人 / 失信被执行人 / 限制消费」三个入口按企业名称检索
- **对应红线**：RL-01（被执行人/失信被执行人）

#### 中国裁判文书网
- **URL**：https://wenshu.court.gov.cn/
- **HTTP 状态**：200（首页可达，检索需登录/验证码）
- **覆盖**：裁判文书、案由、当事人
- **人工操作**：登录后按当事人名称检索，注意同名主体，按案号核对
- **对应红线**：RL-04（劳动争议/仲裁密集）、RL-06（诉讼密集）

#### 人民法院公告网
- **URL**：https://rmfygg.court.gov.cn/
- **HTTP 状态**：200（首页可达，检索需登录/验证码）
- **覆盖**：开庭公告、送达公告
- **人工操作**：按当事人名称检索开庭/送达公告
- **对应红线**：RL-08（退市/立案调查相关公告）

### 5.6 快速验证

装好所有 Key 后，一行命令验证全链路：

```bash
# 1. 探测所有源可用性
./bin/company-dd probe

# 2. 解析一个已知主体
./bin/company-dd resolve "宁德时代"

# 3. 出完整报告（会调用所有可用源）
./bin/company-dd report "宁德时代" --pages 6

# 4. 检查报告
./bin/company-dd show <report_id>
```

### 5.7 不做什么

以下事项明确**不在本工具范围内**（也写进 `DATA-SOURCES.json` 的 `not_included`）：

| 不做 | 原因 |
| --- | --- |
| 验证码绕过 / 滑块破解 / 登录墙绕过 | 不做。被 WAF 拦住的维度如实标记为 `blocked`，转人工 |
| 个人隐私数据（手机号、身份证、住址、开房记录等） | 不做。背调对象是法人主体，不是自然人 |
| 商业数据无授权转售 | 不做。需要深度字段时走官方 CLI/API 并自备 Key |
| 对官方站分布式/高频抓取 | 不做。遵守 robots.txt，限速 0.8 秒/主机 |

---

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
| `COMPANY_DD_HOME` | 数据目录（默认 `~/.company-dd`；`bin/company-dd` 默认用仓库内 `data/`）：报告、原始载荷 |
| `COMPANY_DD_TIMEOUT` / `COMPANY_DD_RETRIES` / `COMPANY_DD_HOST_INTERVAL` | 超时、重试、按主机限速 |
| `COMPANY_DD_UA` | 自定义 UA（SEC 要求声明联系人） |
| `DEVNORS_API_KEY` | 启用 Devnors 中国工商聚合（字段标 partial） |
| `OPENCORPORATES_API_KEY` | 启用境外注册库 |
| `TYC_TOKEN` | 天眼查官方 CLI 授权环境标识；实际登录态由 `tyc login` / `tyc init` 写入 `~/.tyc/config.json` |

密钥只从环境变量读取，不落盘、不进日志、不写进报告。

---

## 10. 目录与运维

```
/workspace/company-dd-mcp/
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

- [x] 天眼查 CLI（tyc）深度适配：已实现真实命令调用；登录后覆盖 RL-01/02/03/05，未登录仍明确 `needs_key`
- [x] 人工回填：`manual_record` 强制保存操作人、方法、URL 或附件，并立即重算报告结论
- [ ] SEC 全文检索接入（8-K item 2.04/4.02/3.01）以细化美股红线
- [ ] 公告正文（PDF）二次核验：标题命中后再读正文，降低误报
- [ ] 集团穿透：母公司/子公司逐层跑规则，输出集团风险传导路径
- [ ] 报告对比：同一主体两次报告的 diff（发版式背调）

License: MIT
