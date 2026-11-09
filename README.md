# 港股筹码跟踪

**用一套命令，把港股筹码结构、日内指标和历史信号整理成可追溯的研究记录。**

默认跟踪美团-W（03690.HK），也可以初始化其他港股。无需配置 API Key，使用 Python 标准库运行；项目同时提供可供 AI 助手调用的 Skill。

> 筹码分布由历史行情和换手率估算，**不是实际持仓明细**。数据、指标与规则输出仅供研究，不构成投资建议。

---

## 它能做什么

| 能力 | 你会得到什么 |
| --- | --- |
| **每日跟踪** | 筹码峰、获利盘、换手、沽空与南向数据（持仓趋势 + 十大成交当日净买卖）、技术状态、宏观流动性评分与事件；生成 Markdown 日报与 CSV 快照 |
| **日内观察** | VWAP、分时成交分布、开盘区间、相对强弱、分钟 uptick 主动买/卖方向估算等指标；显著信号追加到日内日志 |
| **盘中强制风控** | rs_override：带量跌破防线即减半仓（回测验证，美团带量组期望 +4.0pp/胜率 78%，见 backtest-override 报告）；仅美团启用 |
| **宏观流动性** | 美元指数与 USDHKD 的 20 日方向评分；FOMC/非农/CPI/财报事件倒计时与 T-1 预警 |
| **其他港股** | 根据代码生成独立的关键位配置草稿，先观察，再由使用者校准；五个已有标的各带经回测验证的规则 |
| **历史回测** | 单标的信号统计（支持 `--code`）、全历史深度回测（两日确认/含成本/事件研究）、多标的横向验证 |
| **AI Skill** | 让支持 Skill 的助手按项目约定调用命令并解读输出 |

## 快速开始

### 环境要求

- Python **3.10+** 和系统时区数据库；运行时只使用标准库。
- 网络可访问腾讯行情、ETNet 和东方财富。数据源可能超时、限流或变更。
- 本地已在 macOS 测试；CI 配置覆盖 macOS 和 Linux，远端 CI 状态以实际运行结果为准。其他平台尚未验证。

将项目放到本地后，在项目根目录执行：

源码现位于 `hk_stock_tracker/`；此前直接调用根目录 `tracker.py` 等脚本的外部任务，需要改用下面的模块命令或安装后的 `hk-tracker` 命令。

```bash
# 美团：生成每日跟踪报告
python3 -m hk_stock_tracker.tracker daily

# 美团：查看日内结构
python3 -m hk_stock_tracker.tracker intraday
```

也可以从本地源码安装命令行入口（需先安装 pipx）：

```bash
pipx install .
hk-tracker track init --code 3690
hk-tracker track daily --code 3690
hk-tracker --help
```

安装版默认将运行数据写入 `~/.local/share/hk-stock-tracker`，可用 `HK_TRACKER_HOME` 指定其他目录；初始化会生成待人工校准的草稿，不会自动复制本仓库的历史数据和配置。直接运行源码时仍默认使用项目目录。包尚未发布到 PyPI；安装版的 Skill 仍需单独配置。

日报会在终端显示，也会写入 `reports/YYYY-MM-DD.md`，指标快照写入 `data/snapshots.csv`。同一交易日重跑会更新当天记录；日内命令仅在出现显著信号时追加日志。

### 跟踪其他港股

```bash
# 以腾讯控股为例：先生成配置草稿
python3 -m hk_stock_tracker.tracker init --code 0700

# 再运行日报或日内分析
python3 -m hk_stock_tracker.tracker daily --code 0700
python3 -m hk_stock_tracker.tracker intraday --code 0700
```

代码支持 `700`、`0700`、`00700` 和 `700.HK` 等格式。新标的使用 `data/<五位代码>/` 和 `reports/<五位代码>/`，与美团数据分开。

如果配置已存在，`init` 不会覆盖它。仓库中已有的腾讯配置属于历史示例，使用前也应重新核验和校准。

**新生成的关键位需要人工校准。** 草稿状态下，程序不会触发日线 playbook 或基于关键位的日内做 T 信号；但带 `backtest_validated: true` 的规则（来自该标的自身的回测结论）会照常评估输出。完成校准后，需要检查并修改对应 `levels.json`，将 `auto_generated` 设置为 `false`，再配置适合该标的的规则。

已有五个标的完成各自历史回测并写入专属 playbook（美团 03690、腾讯 00700、汇丰 00005、港交所 00388、长和 00001），规则与验证报告一一对应，事件控仓口径见各配置 `event_risk` 字段。长和当前无验证通过的方向信号，仅观察不输出交易立场。

### 历史回测

```bash
# 默认截止到今天；盘中自动排除当天尚未收盘的 K 线
python3 -m hk_stock_tracker.backtest

# 指定截止日期和新的输出文件
python3 -m hk_stock_tracker.backtest --end 2026-10-23 --output /tmp/mt-backtest.md
```

回测报告默认不会覆盖已有文件；确实需要覆盖时可使用 `--overwrite`。旧版 `2026-10-23` 回测使用不同口径，保留为历史记录，不能直接作为信号有效性的依据。所有标的共用 `backtest.py`：`--code 0005` 等参数可切换标的，数据接口按代码自动适配。

### 深度回测与多标的验证

在基础信号统计之上，`backtest_deep.py` 提供更长历史与更多口径：

```bash
# 美团：全历史（2016 起）与 2023 起子窗口双跑、两日确认规则对照、含双边成本净边际、宏观事件研究
python3 -m hk_stock_tracker.backtest_deep

# 其他标的（数据接口与分箱宽度按代码自适应）
python3 -m hk_stock_tracker.backtest_deep --code 0005
```

已完成的验证结果（详见 `reports/backtest-deep-multi-2026-11-06.md` 与各标的明细报告）：

- **信号有效性因标的而异，playbook 不可互借**。含双边成本（0.3%）后的存活信号：汇丰=放量类信号；港交所=深套反弹+放量涨；腾讯=南向增持抄底；长和=无可靠方向信号；美团=放量涨（弱）+南向连减（回避）。
- **两日确认**（放量突破后次日仍站稳）经全历史验证：方向正确但不足以独立盈利，定位为右侧规则的必要条件，需叠加共振过滤。
- **宏观事件冲击分级**：长和 0.94x（基本免疫）、腾讯/汇丰约 1.15x、美团/港交所约 1.24x；各标的的事件控仓口径写入其 `levels.json` 的 `event_risk`。
- 数据口径：qfq 前复权 2016 年之前存在失真（高分红标的早段出现负价格伪影），hfq 后复权序列断裂不可用；统一截取 2016 年起，2016–2020 段幅度偏高 1.3–1.6 倍，方向有效、幅度仅供参考。

多标的横向信号对比见 `reports/backtest-multi-2026-11-06.md`。

### 多标的样本外研究

提前确定股票、样本外起始日期和成本假设，然后运行：

```bash
python3 -m hk_stock_tracker.research --codes 3690 700 --split 2026-06-01 --end 2026-09-30 --cost-bps 15 --output /tmp/hk-research.json
```

`15` 是示例假设，单位为每边基点，并非实际费率；应根据交易渠道、税费、价差和滑点自行估计。信号在收盘后产生，假设下一交易日开盘买入，持有 5、10 或 20 个完整交易日后按开盘价卖出，同一信号和持有期内不重叠交易。结果分样本内、样本外记录单笔净收益和统计，不是组合资金曲线。

此研究只使用日 K 相关信号；南向和沽空数据的实际发布时间尚未核验，未纳入。历史行情可能前复权或被修订，日 K 收盘数据的实际可用时间尚未核验；模拟开盘价不代表实际可成交价格。若根据样本外结果挑选股票或规则，该样本外结果不再独立。结果文件不覆盖已有文件。

研究只统计静态交易日历覆盖且完整连续的持有窗口，缺口或日历未覆盖的窗口会被排除，具体数量见 `excluded_windows`；缺口可能是个股停牌、临时停市或行情缺失，需另行核验。内置日历覆盖 2023–2027 年，并记录了部分已确认的临时停市，但仍需核验未列出的临时安排。

研究使用的筹码分箱宽度默认是 0.5 港元，可通过 `--chip-width` 指定。要对照日报指标，应与标的 `levels.json` 中的 `chip_bin_width` 保持一致。价格收益按行情数据计算，不单独计入现金分红，不能直接视为总收益。可用 `--actions` 输入自行核验的公司行为文件，排除跨事件窗口；该文件需要覆盖研究股票的整个行情区间。格式示例：

2026-10-28 更新后，滚动研究与日报在相同历史数据、价格和分箱宽度下使用相同的获利盘与筹码峰口径。此前生成的研究、回测和观察记录仍属旧口径，不能与新结果直接比较，应重新运行研究；旧观察记录保留为原始历史，不要覆盖。

```json
{"format":"hk-tracker-corporate-actions-v1","stocks":{"00700":{"coverage_start":"2023-01-01","coverage_end":"2026-10-27","events":[{"date":"2025-05-15","type":"cash_dividend"}]}}}
```

日期仅为格式示例，使用前须按公司公告核实并补全；支持 `cash_dividend`、`split`、`rights` 和 `other`。未提供事件文件或文件遗漏事件时，跨事件收益仍可能失真；程序不会验证文件完整性，也不会自动计算分红总收益。计划模式暂不支持该文件。

### 固定数据与提前声明研究计划

可以先保存一份**解析后的日 K 快照**，之后离线复跑同一份数据：

```bash
python3 -m hk_stock_tracker.research_data capture --codes 3690 700 --end 2026-09-30 --output /tmp/hk-snapshot.json
python3 -m hk_stock_tracker.research --codes 3690 700 --split 2026-06-01 --end 2026-09-30 --cost-bps 15 --snapshot /tmp/hk-snapshot.json --output /tmp/hk-replay.json
```

后续抓取可以与上一份快照自动对比，也可以单独对比两份已有快照：

```bash
python3 -m hk_stock_tracker.research_data capture --codes 3690 700 --end 2026-09-30 --previous /tmp/hk-snapshot.json --output /tmp/hk-snapshot-next.json
python3 -m hk_stock_tracker.research_data compare --old /tmp/hk-snapshot.json --new /tmp/hk-snapshot-next.json --output /tmp/hk-snapshot-diff.json
```

快照记录每段请求的复权参数、返回字段、实际选用字段和响应文本的 SHA-256；对比结果列出重叠日期中价格、成交量及换手率的变化与缺失日期。历史快照可能没有这些来源字段。不同日期窗口若混用返回字段，研究和逐日观察会拒绝运行；字段名不等于已经证实的复权口径，变化也不能自动判定为公司行为或数据修订。

对于未来的样本外研究，可以在开始前创建计划（日期仅为示例，运行时样本外起始日期必须晚于当天）：

```bash
python3 -m hk_stock_tracker.research_data plan --codes 3690 700 --split 2027-01-01 --end 2027-12-31 --cost-bps 15 --output /tmp/hk-plan.json
# 截止日期到达后，抓取数据，再使用 --plan 和 --snapshot 运行
python3 -m hk_stock_tracker.research --plan /tmp/hk-plan.json --snapshot /tmp/hk-snapshot-2027.json --output /tmp/hk-planned-result.json
```

计划记录股票、时间范围、成本、分箱宽度和代码哈希；代码变更后不能直接用旧计划运行。创建计划时可加 `--bundle /tmp/hk-plan-source.zip` 保存关联源码；核对 ZIP 哈希后在独立目录解压，可用归档内的脚本继续旧计划。若计划文件写入失败，已生成的源码归档可能需要手动清理。快照与计划默认拒绝覆盖，结果记录快照的 SHA-256。**本地文件、哈希和时间戳均可事后修改**；要证明计划事先确定，需要在开始前进行可信的外部时间戳或公开存证。快照并非原始响应，也不能证明历史行情在当时已发布，或替代逐日的实时信号记录。

### 宏观流动性与事件日历

日报包含宏观流动性节：美元指数（东财）与 USDHKD（腾讯）的 20 日方向各计 −1/0/+1，合成评分（当前 ±2 范围）只作为仓位口径的背景音，不直接触发买卖；评分 ≥+2 标注顺风、≤−2 标注逆风。美债十年期收益率与 HIBOR 暂无稳定免费数据源，代码中留有 TODO。

事件日历存于 `data/macro_events.json`（FOMC、非农、CPI、财报，可手工校准），日报显示未来两个事件的倒计时，T-1 日出预警（如非农前控仓口径），事件当日在报告中注记。事件次日波动幅度因标的而异（0.94x–1.25x），分级口径见各标的配置 `event_risk` 与深度回测报告。

### 逐日记录信号

样本外观察期开始后，每个交易日确认收市，再运行一次：

```bash
# 将输出路径中的日期替换为当天日期；文件已经存在时不会覆盖
python3 -m hk_stock_tracker.observe --plan /tmp/hk-plan.json --output /tmp/observation-2027-01-04.json
```

记录包含计划及其哈希、代码哈希、各标的抓取时间、本次取得的完整解析日 K、换手估算日期及当天信号。是否首次触发是用本次抓取的数据计算当天与前一交易日的状态，不是对前一日观察文件的核验。当天日 K 未返回、计划以外的日期、盘中或代码与计划不一致时，程序拒绝记录；某只标的缺失时整次运行失败，不会把缺失信号当作无信号。运行依赖网络，不会自动执行，也不会自动补录漏掉的日期。

交易日历不覆盖异常停市或个股停牌；价格数据也可能延迟、修订或前复权。记录不能证明成交，且本地文件和时间戳不能独立证明记录产生的时间。实际长期跟踪时，应保留每日文件并进行外部时间存证；这些观察记录目前不会自动汇总为成交回测。

完成若干天观察后，可以复盘已保存文件：

```bash
python3 -m hk_stock_tracker.replay /path/to/observations/*.json --output /tmp/hk-observation-review.json --markdown /tmp/hk-observation-review.md
python3 -m hk_stock_tracker.research_data check-plan --plan /tmp/hk-plan.json
```

复盘使用观察记录中当时保存的信号，检查计划一致性和预期交易日缺口，列出已完成、未到期及缺少记录的模拟收益；也会用最后一份快照重算历史信号并列出差异。收益使用退出日同一份快照内的入场和退出价格；如果入场价与入场日快照不一致，标记 `price_revision` 而不计算收益。即使价格一致，也不能证明期间不存在公司行为。结果按信号分别统计，允许重叠，不能当成组合收益；收市后保存的开盘价仍是模拟价格，不保证成交。静态日历的缺口不能直接判定为漏记录或停牌。

复盘会核对当前代码与计划指纹；版本不匹配时，请使用计划对应的源码归档。新建计划的源码归档包括复盘程序；本次更新之前创建的归档可能不含该程序。JSON 和 Markdown 发生普通写入错误时会尽力清理由本次运行创建的文件；突然断电或进程被强制终止不保证两个文件同时完成。

可以对同一快照检查模型假设的影响：

```bash
python3 -m hk_stock_tracker.sensitivity --snapshot /tmp/hk-snapshot.json --code 3690 --widths 0.25 0.5 1 --windows 252 500 --output /tmp/hk-sensitivity.json
```

敏感性结果是不同分箱宽度和历史窗口下的模型输出，不代表参数优劣或真实持仓。研究结果另含全交易日基准及收益四分位数；至少 30 笔交易时附加固定随机种子、块长为 3 的移动块自助法均值区间，样本更少则不计算区间。交易间隔可能不均，区间只是探索性估计，不保证覆盖率。基准样本可重叠，信号交易则按各信号和持有期去重，两者不能直接作为统计显著性证明；没有进行多重检验校正或计算组合资金曲线。

## 使用 Skill

项目内的 [hk-stock-tracker Skill](skills/hk-stock-tracker/SKILL.md) 提供命令调用与解读约定。将它安装到你的 Agent 支持的技能目录后，助手可以根据请求调用项目脚本。

Skill 默认从 `~/hk-stock-tracker` 查找项目；如果项目位于其他位置，在运行 Agent 的环境中设置：

```bash
export HK_STOCK_TRACKER_HOME="/你的项目路径/hk-stock-tracker"
# 原 MEITUAN_TRACKER_HOME 仍可使用，新变量优先。
```

Skill 依赖本项目的 Python 脚本和数据配置，需要一并部署。不同 Agent 的技能安装方式以其自身文档为准。

## 数据与规则边界

### 数据从哪里来

| 数据 | 来源 | 注意事项 |
| --- | --- | --- |
| 港股报价、日 K、分钟数据 | 腾讯行情 | 字段、覆盖范围与可用性可能变化 |
| 沽空数据 | ETNet | 历史长度有限；备用图表金额为估算，报告会标注；收盘后高峰期偶发超时 |
| 南向持仓变化 | 东方财富 | 存在数据延迟，历史实际发布时间仍需核验 |
| 美元指数 | 东方财富 | 用于宏观流动性评分 |
| 美元港元汇率 | 腾讯行情 | 联系汇率 7.75–7.85 区间内的位置解读 |
| 宏观事件日历 | 本地配置 `data/macro_events.json` | 非农/CPI 日期为程序生成的近似值，应手工校准 |
| 港股交易日历 | 香港交易所公告 | 当前内置 2023–2027 年证券市场常规休市和半日市，以及部分已确认临时停市 |

港股时间采用 `Asia/Hong_Kong`。普通交易日到 **16:10**、半日市到 **12:10** 后才视为收市，覆盖最晚结束的收市竞价。未覆盖年份不会猜测当天收市状态；极端天气、临时停市和个股停牌仍需自行核验。

历史数据与报告目前保留在项目中作为公开候选。公开分发前，请核实各来源的使用及再分发条款。更多细节见 [数据来源与回测局限](docs/DATA_PROVENANCE.md)。

### 规则怎样触发

- 盘中不会触发依赖 `close_*` 的日线收盘条件；换手率使用估算值时，不触发依赖换手的条件。
- 标记 `"action": "entry"` 的规则，在赔率不可计算或小于 1 时不会触发；其他规则不受该门槛约束。
- 单日收盘突破不等于连续两日站稳；深度回测已验证两日确认方向有效但不足以独立盈利，作为右侧规则的必要条件并需叠加共振过滤（见 `reports/backtest-meituan-deep-2026-11-06.md`）。
- 回测将南向及沽空信号保守后移一个交易日，但数据真实发布时间、前复权、交易成本和收盘成交可行性尚未完全核验。

规则与赔率不是收益保证。程序不执行交易，也不连接券商账户。

### 文件写入与恢复

日报和快照写入前会记录待完成事务；如果中途失败，下次运行同一标的任务时会尝试恢复。若目标文件被外部修改，程序会停止并保留恢复记录供检查。写入时，不使用锁的其他程序仍可能短暂看到两个文件不同步；恢复机制不保证断电后的所有文件系统行为。

同一标的的初始化、日报和日内任务使用非阻塞进程锁；已有任务运行时，新任务会退出并提示。不同标的可以并行。

## 项目结构

```text
hk_stock_tracker/             Python 源码与命令入口
  tracker.py                  日报、日内分析与标的初始化
  backtest.py                 信号历史统计（支持 --code 多标的）
  backtest_deep.py            全历史/两日确认/含成本/事件研究深度回测
  research.py                 多标的固定规则研究
  research_data.py            行情快照与研究计划
  observe.py / replay.py      逐日观察与复盘
  sensitivity.py              模型敏感性分析
  hk_calendar.py              港交所证券市场交易日历
docs/                         数据来源及历史跟踪日志
data/                         标的配置、快照、日内日志与宏观事件日历
reports/                      日报和历史研究报告
skills/hk-stock-tracker/       AI 助手技能说明
tests/                        离线回归测试
```

## 开发与测试

```bash
python3 -B -m unittest discover -s tests -v
```

测试使用模拟数据，不访问行情源，也不修改现有快照或报告。贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目有权许可的代码和原创文档采用 [MIT License](LICENSE)。第三方行情、内容和商标不因本项目许可证而获得再分发许可。

港交所参考资料：[2026 年假期公告](https://www.hkex.com.hk/-/media/HKEX-Market/Services/Circulars-and-Notices/Participant-and-Members-Circulars/SEHK/2025/ce_SEHK_CT_075_2025.pdf) · [2027 年假期公告](https://www.hkex.com.hk/-/media/HKEX-Market/Services/Circulars-and-Notices/Participant-and-Members-Circulars/SEHK/2026/ce_SEHK_CT_077_2026.pdf) · [证券市场交易时间](https://www.hkex.com.hk/Services/Trading-hours-and-Severe-Weather-Arrangements/Trading-Hours/Securities-Market?sc_lang=en)
