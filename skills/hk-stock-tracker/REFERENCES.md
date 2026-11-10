# 外部量化策略参考备忘录

> 2026-11-06 整理。GitHub 上筛出的硬核量化策略仓库（约 200+ 候选中选出），标准是"有真策略逻辑 + 有严谨验证方法"，排除通用框架/教程/书单/纯黑盒 ML 玩具。供后续方法论参考，不构成推荐。

## 精选清单

### 1. noterminusgit/statarb（23★）
https://github.com/noterminusgit/statarb
有真实生产血统的美股统计套利系统：20+ alpha、PCA+Barra 风险分解、含成本/冲击/借券费的组合优化、四套仿真引擎（日频/订单级/日内/全周期）。函数级规格文档。
**价值**：生产级 stat-arb 架构范本。**局限**：需付费数据（Barra 因子、分析师预期、short locate）才能跑。

### 2. ssanin82/dtc-stat-arb（1★）
https://github.com/ssanin82/dtc-stat-arb
crypto 永续统计套利研究：预注册 22 个假设 + 真实账户实测成本 + 完整公开的负面结果。核心发现：杀死策略的是资金费率（吃掉反转信号 46% 毛 edge）而非手续费；产出可迁移的"成本惩罚倍数"指标（日频 5-6×、小时频 63-100×）。
**价值**：防自欺方法论教科书。**与我们项目的共鸣**：含成本后仅一个信号存活，同构教训。

### 3. chrispyroberts/imc-prosperity-3（169★）
https://github.com/chrispyroberts/imc-prosperity-3
IMC 做市竞赛全球第 7/美国第 1 全部五轮代码：探测单识别隐性做市商、ETF 篮子套利、期权定价。公开承认数据泄漏导致排名崩盘的教训。
**价值**：第三方验证的模拟盘记录（比自述回测可信）；微观结构巧思。
**同类**：[TimoDiehm/imc-prosperity-3](https://github.com/TimoDiehm/imc-prosperity-3)（594★，全球第 2，writeup 更系统）、[ericcccsliu/imc-prosperity-2](https://github.com/ericcccsliu/imc-prosperity-2)（252★）。

### 4. kieranjwood/trading-momentum-transformer（644★）
https://github.com/kieranjwood/trading-momentum-transformer
Oxford-Man Institute 正式发表：Transformer 注意力+变点检测，直接输出仓位、Sharpe（含成本）为训练目标；100 个期货 1995-2020；注意力权重可解释。
**对照**：作者新作 [deepm](https://github.com/kieranjwood/deepm)（GNN+组合层 Sharpe 目标+EVaR 惩罚）声称全面超越。

### 5. davidxu277/alpha-timing（114★）
https://github.com/davidxu277/alpha-timing
市场状态 nowcaster（RF 分 Bull/Bear/Sideways/Crisis）路由到专科预测器，双阈值迟滞+波动率目标仓位。
**最值得抄的设计**：GitHub Actions 每日追加信号到**哈希链 track_record.jsonl**，防篡改前向实盘记录（已跑 56 交易日）——我们的日报/JOURNAL 缺不可篡改性，可借鉴。
**局限**：NUS 学生作者，前向样本仅 3 个月，6 标的均为大盘科技股（高相关）。

### 6. lambdaclass/options_portfolio_backtester（284★）
https://github.com/lambdaclass/options_portfolio_backtester
期权组合回测（Rust 内核），含 Spitznagel 式尾部对冲预设。
**价值**：`docs/METHODOLOGY.md` 是最诚实的期权回测陷阱清单，每条标注"改变过我们自己的数字"（入场日历运气可让年化差 1.5pp；脏期权链让 2008 年对冲比买入持有更差）；数据文件 SHA-256 锁定。

### 7. hugo2046/QuantsPlaybook（6856★）
https://github.com/hugo2046/QuantsPlaybook
100+ 国内券商金工研报代码复现：RSRS 择时、聪明钱、APM、**筹码分布因子**（与我们换手衰减模型同族，可互相校验）、STR 凸显性等。
**用法**：策略想法金矿 + 复现起点；复现质量不均，缺严格样本外，不当已验证结论。依赖聚宽/tushare 数据，A 股语境。

## 荣誉提名

- [JerBouma/AlgorithmicTrading](https://github.com/JerBouma/AlgorithmicTrading)（1105★）：Optiver 员工评审的套利分析（双重上市/期权/统计套利）；2023-08 停更。作者诚实声明"零售无 edge"。
- [tradermonty/earnings-trade-backtest](https://github.com/tradermonty/earnings-trade-backtest)（5★）：PEAD 财报策略；亮点是数据源质量审计（EODHD 财报日期准确率仅 44% → FMP 99.7%）——事件策略最易死在日期错位。

## 可借鉴到本项目的两件事

1. **哈希链前向记录**（来自 alpha-timing）：给日报/snapshots 加哈希链，变成可公开验证的跟踪记录。
2. **筹码分布因子交叉验证**（来自 QuantsPlaybook）：用其复现口径校验我们的换手衰减模型输出。

## 排除说明

另类数据（卫星/供应链）未找到达标仓库；纯 LSTM 股价预测类全部排除；框架类（hftbacktest/nautilus 等）按任务要求排除；pysystemtrade、Robot-Wealth 等因近 2-5 年无维护排除。

---

## 补充：港股/A股方向专项调研（2026-11-07）

结论：港股方向 0 个达标；A股方向 3 个达标 + 2 个提名。合格率远低于美股方向。

### 达标

1. **yupoet/aurumq-rl**（56★）https://github.com/yupoet/aurumq-rl — A股 RL 选股：296 个量价因子（Alpha101+Alpha191）+ 13 个 A 股特有因子族（龙虎榜/北向/筹码分布）。最硬的是 26 个 phase 的失败史全公开（如"截面 rank-z 归一化销毁跨年幅度信号"）。生产系统每日运行，T1_hit 55.8% 如实披露。学其研究方法与工程封装，而非直接用信号。
2. **ericxuzhesheng/VPIN-Based-Timing-Strategy-for-Chinese-Government-Bond-Futures**（4★）https://github.com/ericxuzhesheng/VPIN-Based-Timing-Strategy-for-Chinese-Government-Bond-Futures — VPIN 订单流毒性用于国债期货择时。预注册协议 + 独立核验 + 缺陷旧版保留不删。标的是国债期货，作为"中国市场微观结构研究法"参考。
3. **zhuleimed/etf-daily-sync-and-backtest**（37★）https://github.com/zhuleimed/etf-daily-sync-and-backtest — A股 ETF 轮动全流程。防前视"纪律红线"置顶（信号 T 日收盘→T+1 开盘执行+涨跌停检查），策略对比/优化历史/Bug 修复全透明。策略本身无独家 alpha，价值在工程纪律。

### 提名

- **phonegapX/alphasickle**（432★）：沪深300指增标准流水线（中性化/IC/分层/Barra 优化），2014-2019 年化超额 8.3%；2024-03 停更，样本外无从验证。
- **robertquant/python_jointquant**（16★）：聚宽社区经典流派最小合集（ETF 轮动/AH 溢价/小市值/红利），了解散户圈在玩什么；无样本外纪律。

### 生态画像（诚实判断）

- **港股开源策略代码接近空白**：alpha 的公开载体是券商研报（如开源金工魏建榕组的 CCASS 持仓集中度策略），只有 PDF 无官方代码。路径=读研报→自己复现→数据源 akshare/longbridge。
- **A股量大但分布极端**：生态被数据源/框架/AI 看盘工作台三类吃掉，真策略仓库占比很小；优质资产以券商金工研报为锚（QuantsPlaybook 是唯一规模化桥梁）；实盘代码锁在聚宽/掘金平台围墙里；打板/龙虎榜流派无可信开源回测；北向跟随策略 2024-08 数据停披后大面积失效。
- **任何 A 股开源策略的三条快筛**：信号与执行时点是否分离、是否处理涨跌停无法成交、是否扣双边成本——能筛掉九成项目。
