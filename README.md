# 智能混合型合约交易机器人 (AI-Hybrid Futures Trader)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

这是一个用于加密货币合约市场（Binance Futures）的自动化量化交易机器人。

它的核心是一个**混合策略引擎**，将大型语言模型（LLM）的宏观分析能力与传统的算法交易（TA）信号相结合，在一个统一的风险管理框架下运行。

## 目录

1.  [项目介绍](#1-项目介绍)
2.  [核心功能](#2-核心功能)
3.  [策略解释](#3-策略解释)
    * [🤖 AI 核心策略](#-ai-核心策略)
    * [📈 算法策略](#-算法策略)
    * [🛡️ 仓位管理与风控行为](#-仓位管理与风控行为)
4.  [部署与使用](#4-部署与使用)
    * [环境准备](#环境准备)
    * [配置文件 (.env)](#配置文件-env)
    * [依赖库 (requirements.txt)](#依赖库-requirementstxt)
    * [启动真实交易](#启动真实交易)
    * [启动模拟测试](#启动模拟测试)
    * [访问监控面板](#访问监控面板)
5.  [关键参数解释](#5-关键参数解释)
    * [A. 基础与API配置](#a-基础与api配置)
    * [B. AI 核心配置](#b-ai-核心配置)
    * [C. 核心风控与仓位](#c-核心风控与仓位)
    * [D. 算法 - 趋势策略](#d-算法---趋势策略)
    * [E. 算法 - 突破策略](#e-算法---突破策略)
    * [F. 算法 - 震荡策略](#f-算法---震荡策略)
    * [G. 算法 - 仓位管理](#g-算法---仓位管理)

---

## 1. 项目介绍

本项目旨在实现一个 7x24 小时全自动运行的合约交易系统。它不是一个单一策略的机器人，而是一个集成了多种交易范式的**策略管理器**。

* **AI 驱动:** 机器人会定时或在市场发生异动时，调用 AI（支持 OpenAI, Azure, DeepSeek）对当前市场数据进行全面分析，并生成结构化的交易信号（做多/做空/中性、置信度、建议入场/止损/止盈价）。
* **算法辅助:** 当 AI 处于“中性”观望状态时，系统会回退到传统的算法策略，自动寻找趋势回调、通道突破或均值回归的交易机会。
* **性能反馈:** 系统会持续跟踪 AI 信号的表现（包括模拟和真实交易），计算出一个“绩效分数”。这个分数会反馈给 AI,使其能够进行**自我纠错**（例如，在表现不佳时被提示“更加保守”）。
* **风险优先:** 所有的交易决策（无论是 AI 还是算法）都必须通过一个严格的风险管理层，该层负责仓位规模计算、动态止损和订单执行。

## 2. 核心功能

* **多策略引擎:** 同时运行 AI 核心策略、趋势跟踪、布林带突破和震荡均值回归策略。
* **动态 AI 供应商:** 支持在 `.env` 中一键切换 `azure`, `openai` 或 `deepseek` 作为 AI 分析服务商。
* **混合订单执行:** AI 策略支持“市价单”（Taker）立即成交，或“限价单”（Maker）挂单等待回调，并自动监控和取消失效的挂单。
* **AI 防御性调整:** 当 AI 信号与真实持仓方向相反时，系统**不会立即平仓**（防止被噪音洗盘），而是立即触发“防御性止损”，大幅收紧止损位以保护利润。
* **高级风控:**
    * **动态追踪止损:** 结合 ATR 和 Chandelier Exit（2R 利润激活）自动上移/下移止损。
    * **金字塔加仓:** 在趋势确认的盈利仓位上自动加仓（最多2次）。
    * **保证金管理:** 基于总权益和单笔风险百分比（`FUTURES_RISK_PER_TRADE_PERCENT`）自动计算仓位大小，并检查是否超过保证金上限（`MAX_MARGIN_PER_TRADE_RATIO`）。
* **实时 Web 监控:** 内置 `aiohttp` web 服务，提供一个实时仪表盘，显示当前持仓、浮动盈亏、策略表现（胜率、盈亏比）、AI 信号、模拟仓位和实时K线图表。
* **高保真模拟器 (`paper_trader.py`):** 提供一个独立的 `paper_trader.py` 脚本。它 100% 继承了 `FuturesTrendTrader` 的所有策略逻辑，但通过重写底层的交易所交互方法，将所有真实下单请求拦截并转发到一个 `MockExchange` 类。这使其成为一个用于前瞻性测试（Forward-Testing）的完美工具。

---

## 3. 策略解释

系统在空仓时，会**优先等待 AI 信号**。如果 AI 信号为 `neutral`，系统则会**自动启用算法策略**来寻找机会。

### 🤖 AI 核心策略

* **行为模式:** AI 作为策略的“宏观大脑”。它接收 K 线、RSI、MACD、布林带、ADX、ATR 和恐惧贪婪指数等全方位数据，并提供一个结构化的交易计划（包括入场、止损、止盈）。
* **触发机制 (事件驱动):**
    1.  **定时器:** 每 `AI_ANALYSIS_INTERVAL_MINUTES` 分钟进行一次常规“心跳”分析。
    2.  **技术指标异动 (立即触发):** 当 15m K 线上发生 **MACD 交叉**、**RSI 越界**或 **K 线突破布林带**时，立即触发 AI 分析。
    3.  **市场剧烈波动 (立即触发):** 当 1h K 线价格变动超过 `AI_VOLATILITY_TRIGGER_PERCENT` 时，立即触发 AI 分析。
* **执行:**
    * **风险检查:** 信号必须通过 `AI_MIN_RISK_REWARD_RATIO` (风险回报比) 检查。
    * **绩效检查:** AI 的历史绩效分数 (`historical_performance_score`) 必须大于 `AI_CONFIDENCE_THRESHOLD` 且 `AI_ENABLE_LIVE_TRADING` 为 `True` 时，才执行真实交易。
    * **下单:** 根据 `AI_ORDER_TYPE`（`market` 或 `limit`）执行市价或限价单。

### 📈 算法策略

当 AI 未给出开仓信号时，以下三个算法策略会并**行运行**以捕捉机会：

#### 策略 1: 趋势回调 (Pullback Entry)

* **策略算法:** 趋势跟踪 + 均值回归 (EMA 通道)。
* **行为模式:**
    1.  **识别趋势:** `_detect_trend` 使用 `5m` 和 `15m` 的 EMA 组合判断宏观趋势（例如，`5m` 均线向上且 `15m` 均线也向上，判断为 `uptrend`）。
    2.  **等待回调:** 在确认的上升趋势中，等待价格回调至 `10-20` EMA 动态支撑区 (`_check_entry_signal`)。
    3.  **确认动能:** 调用 `_confirm_momentum_rebound`。此函数会检查 7 周期 RSI 是否已连续 3 根 K 线（由 `ENTRY_RSI_CONFIRMATION_BARS` 定义）**持续回升**（多头）或**持续回落**（空头），以确认回调结束，动能恢复。
    4.  **(可选) 质量过滤:** `_analyze_pullback_quality` 会比较回调浪的成交量和主升浪的成交量，如果回调量过大（`PULLBACK_MAX_VOLUME_RATIO`），则视为危险信号并放弃入场。

#### 策略 2: 动能突破 (Breakout Entry)

* **策略算法:** 波动率扩张 (Bollinger Band Squeeze)。
* **行为模式:**
    1.  **识别挤压:** `_check_breakout_signal` 的核心是 `ENABLE_BBAND_SQUEEZE_FILTER`。系统会持续监控布林带带宽，当带宽收缩到 `BBAND_SQUEEZE_THRESHOLD_PERCENTILE` (例如，过去120根K线中 25% 的最低水平) 时，识别为“挤压”状态。
    2.  **等待突破:** 在挤压状态后，等待 K 线**首次**放量收盘于布林带通道**之外**。
    3.  **确认动能:**
        * **成交量:** 突破 K 线的成交量必须大于 `BREAKOUT_VOLUME_MULTIPLIER` 倍的平均成交量。
        * **RSI:** 突破 K 线的 RSI 必须大于 `BREAKOUT_RSI_THRESHOLD`（例如 50）以确认动能方向。
    4.  **执行:** 这是一个高动能交易，使用**单独的止损逻辑** (`_manage_breakout_momentum_stop`)，即按价格高/低点的百分比 (`BREAKOUT_TRAIL_STOP_PERCENT`) 追踪止损。

#### 策略 3: 均值回归 (Ranging Entry)

* **策略算法:** 反趋势 (Bollinger Band Fade)。
* **行为模式:**
    1.  **识别震荡:** `_check_ranging_signal` 仅在 `_detect_trend` 判断市场为 `sideways` (盘整) 且 ADX 指数**低于** `RANGING_ADX_THRESHOLD` (例如 20，表示无趋势) 时激活。
    2.  **反向开仓:** 当价格触及 `RANGING_BBANDS_PERIOD` (例如 20) 的布林带**上轨**时，执行**做空**；触及**下轨**时，执行**做多**。
    3.  **止盈:** 止盈目标固定为 `RANGING_TAKE_PROFIT_TARGET`，通常是布林带**中轨** (`'middle'`)。
    4.  **止损:** 初始止损由 `RANGING_STOP_LOSS_ATR_MULTIPLIER` 定义。

### 🛡️ 仓位管理与风控行为

这些行为应用于所有（AI 和算法）的持仓：

* **金字塔加仓 (`_check_and_execute_pyramiding`):**
    当仓位盈利超过 `dyn_pyramiding_trigger`（动态R倍数）时，自动加仓（`PYRAMIDING_ADD_SIZE_RATIO`）。最多加仓 `PYRAMIDING_MAX_ADD_COUNT` 次。
* **动态追踪止损 (`_update_trailing_stop`):**
    使用 ATR 和动态乘数（`dyn_atr_multiplier`）来计算止损位，随价格移动而不断收紧。
* **吊灯止损 (`_update_trailing_stop`):**
    当利润达到 `CHANDELIER_ACTIVATION_PROFIT_MULTIPLE`（例如 2R）时，止损逻辑切换为更激进的“吊灯止损”，从 `CHANDELIER_PERIOD` 周期内的最高/最低点回撤 `CHANDELIER_ATR_MULTIPLIER` 倍 ATR。
* **趋势衰竭预警 (`_check_and_manage_trend_exhaustion`):**
    如果 ADX 指数在达到 `EXHAUSTION_ADX_THRESHOLD` 高位后，连续 `EXHAUSTION_ADX_FALLING_BARS` 根 K 线**持续下降**，视为趋势衰竭信号。系统会立即将止损移动到**盈亏平衡点**以锁定利润。
* **危险信号预警 (`_check_reversal_danger_signal`):**
    如果持仓期间，突然出现一根**巨量** (`REVERSAL_ALERT_VOLUME_MULTIPLE`) 且**大实体** (`REVERSAL_ALERT_BODY_ATR_MULTIPLIER`) 的**反向K线**，视为危险信号，立即触发“防御性止损” (`_apply_defensive_stop_loss`)。

---

## 4. 部署与使用

### 环境准备

1.  克隆本仓库:
    ```bash
    git clone https://github.com/hzy11fk/AI-Hybrid-Futures-Trader.git
    cd AI-Hybrid-Futures-Trader
    ```
2.  创建并激活 Python 虚拟环境 (推荐 Python 3.10+):
    ```bash
    python -m venv venv
    # Windows
    .\venv\Scripts\activate
    # macOS/Linux
    source venv/bin/activate
    ```
3.  安装依赖库
    ```bash
    pip install -r requirements.txt
    ```
4.  **(重要)** 确保项目根目录下存在 `logs` 和 `data` 两个文件夹。日志文件 (`logs/trading_system.log`) 和策略状态文件 (`data/position_...json`, `data/profit_...json`, `data/ai_...json`) 会存储在这里。如果不存在，请手动创建：
    ```bash
    mkdir logs data
    ```

### 配置文件 (.env)
    vi .env
### 启动真实交易
    python main.py
### 启动模拟测试
    python paper_trader.py
### 访问监控面板
    启动 main.py（真实交易）后，打开浏览器并访问：
    http://<您的服务器IP或域名>:58182
## 5. 关键参数解释
| 参数名                               | 中文注释和功能说明                                                                                                                                                                                             |
| :----------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A. 基础与API配置 (Settings)** |                                                                                                                                                                                                                  |
| `USE_TESTNET`                      | **是否使用测试网**: `True` 使用币安测试网环境，`False` 使用实盘环境。                                                                                                                               |
| `FUTURES_SYMBOLS_LIST`             | **交易对列表**: 您希望机器人运行的合约列表。格式: `["BNB/USDT:USDT", "ETH/USDT:USDT"]`。                                                                                                                |
| `FUTURES_INITIAL_PRINCIPAL`        | **初始本金**: 用于纸上交易 (`paper_trader.py`) 的起始资金，也用于Web UI计算总盈亏率。                                                                                                                   |
| `BINANCE_API_KEY`                  | **币安实盘 API Key**: 从 `.env` 文件读取。                                                                                                                                                       |
| `BINANCE_SECRET_KEY`               | **币安实盘 Secret Key**: 从 `.env` 文件读取。                                                                                                                                                   |
| `BINANCE_TESTNET_API_KEY`          | **币安测试网 API Key**: 从 `.env` 文件读取。                                                                                                                                                     |
| `BINANCE_TESTNET_SECRET_KEY`       | **币安测试网 Secret Key**: 从 `.env` 文件读取。                                                                                                                                                 |
| `BARK_URL_KEY`                     | **Bark 推送 Key**: 用于发送 iOS 通知。从 `.env` 文件读取。                                                                                                                                         |
| **B. AI 核心配置 (Settings)** |                                                                                                                                                                                                                  |
| `ENABLE_AI_MODE`                   | **启用 AI 模式**: `True` 启用 AI 决策，`False` 仅使用算法策略。                                                                                                                                     |
| `AI_PROVIDER`                      | **AI 服务商**: 在 `.env` 中设置，可选 `azure`, `openai`, `deepseek`。                                                                                                                               |
| `AZURE_OPENAI_ENDPOINT`            | **Azure 端点**: Azure OpenAI 服务的 URL（如果使用 Azure）。                                                                                                                                     |
| `AZURE_OPENAI_KEY`                 | **Azure API Key**: Azure OpenAI 服务的密钥（如果使用 Azure）。                                                                                                                                  |
| `AZURE_OPENAI_MODEL_NAME`          | **Azure 模型部署名**: Azure OpenAI 上的模型部署名称（如果使用 Azure）。                                                                                                                             |
| `OPENAI_API_KEY`                   | **OpenAI/DeepSeek API Key**: OpenAI 或 DeepSeek 的密钥。                                                                                                                                       |
| `OPENAI_MODEL_NAME`                | **OpenAI/DeepSeek 模型名**: 要使用的模型名称，例如 `gpt-4-turbo`, `deepseek-chat`。                                                                                                                  |
| `OPENAI_API_BASE`                  | **OpenAI 兼容 API Base URL**: 如果使用 DeepSeek 或其他兼容 API，在此设置其 Base URL。                                                                                                               |
| `AI_ANALYSIS_INTERVAL_MINUTES`     | **AI 定时分析间隔**: AI 常规分析的频率（分钟）。                                                                                                                                                  |
| `AI_CONFIDENCE_THRESHOLD`          | **AI 绩效分数阈值**: AI 历史绩效分数必须高于此值才能进行真实交易（0-100）。                                                                                                                          |
| `AI_ENABLE_LIVE_TRADING`           | **!!! AI 实盘开关 !!!**: `True` 且满足绩效阈值时，AI 才允许实盘下单，`False` 则 AI 只进行模拟交易。                                                                                                   |
| `AI_STATE_DIR`                     | **AI 状态目录**: 存储 AI 绩效分数文件的目录 (`data`)。                                                                                                                                           |
| `AI_PERFORMANCE_LOOKBACK_TRADES`   | **AI 绩效回看交易数**: 计算 AI 绩效分数所使用的最近交易笔数。                                                                                                                                      |
| `AI_MIN_RISK_REWARD_RATIO`         | **AI 最小风险回报比**: AI 信号的（建议止盈 / 建议止损）必须大于此值才会被接受。                                                                                                                     |
| `AI_ORDER_TYPE`                    | **AI 下单类型**: AI 开仓时使用的订单类型，可选 `'market'` (市价) 或 `'limit'` (限价)。                                                                                                               |
| `AI_LIMIT_ORDER_CANCEL_THRESHOLD_PERCENT` | **AI 限价单取消阈值 (%)**: 当市价偏离 AI 挂单价超过此百分比时，自动取消该挂单。                                                                                                                    |
| **C. 核心风控与仓位 (FuturesSettings)** |                                                                                                                                                                                                                  |
| `FUTURES_LEVERAGE`                 | **合约杠杆**: 在交易所为交易对设置的杠杆倍数。                                                                                                                                                   |
| `FUTURES_MARGIN_MODE`              | **保证金模式**: `'isolated'` (逐仓) 或 `'cross'` (全仓)。                                                                                                                                      |
| `FUTURES_RISK_PER_TRADE_PERCENT`   | **单笔风险百分比**: **[核心风控]** 每笔交易允许的最大亏损占总权益的百分比，用于自动计算仓位大小。                                                                                                       |
| `MAX_MARGIN_PER_TRADE_RATIO`       | **单笔最大保证金比例**: **[核心风控]** 单笔交易占用的保证金不能超过总权益的此比例，防止单仓过重。                                                                                                      |
| `MIN_NOMINAL_VALUE_USDT`           | **最小名义价值 (USDT)**: 如果风险计算出的仓位名义价值小于此值，会自动放大到此值开仓。                                                                                                                 |
| `FUTURES_STATE_DIR`                | **策略状态目录**: 存储持仓状态 (`position_*.json`) 和盈亏历史 (`profit_*.json`) 文件的目录 (`data`)。                                                                                               |
| `USE_ATR_FOR_INITIAL_STOP`         | **使用 ATR 计算初始止损**: `True` 使用 `INITIAL_STOP_ATR_MULTIPLIER` 计算，`False` 使用固定百分比（当前未实现百分比逻辑）。                                                                               |
| `INITIAL_STOP_ATR_MULTIPLIER`      | **初始止损 ATR 倍数**: 用于趋势和 AI 策略开仓时的初始止损计算。                                                                                                                                     |
| `ENABLE_FUNDING_FEE_SYNC`          | **启用资金费率同步**: `True` 定期从交易所同步资金费率并计入总盈亏。                                                                                                                                  |
| `FUNDING_FEE_SYNC_INTERVAL_HOURS`| **资金费率同步间隔 (小时)**: 同步资金费率的频率。                                                                                                                                                |
| **D. 算法 - 趋势策略 (Settings)** |                                                                                                                                                                                                                  |
| `TREND_SIGNAL_TIMEFRAME`           | **趋势信号时间框架**: 主要用于判断趋势方向和寻找入场点的 K 线周期 (例如 `'5m'`)。                                                                                                                      |
| `TREND_FILTER_TIMEFRAME`           | **趋势过滤时间框架**: 用于判断宏观趋势背景的较大 K 线周期 (例如 `'15m'`)。                                                                                                                          |
| `TREND_SHORT_MA_PERIOD`            | **信号短周期均线**: 用于 `TREND_SIGNAL_TIMEFRAME` 上的短周期移动平均线 (例如 `7`)。                                                                                                                |
| `TREND_LONG_MA_PERIOD`             | **信号长周期均线**: 用于 `TREND_SIGNAL_TIMEFRAME` 上的长周期移动平均线 (例如 `21`)。                                                                                                               |
| `TREND_FILTER_MA_PERIOD`           | **过滤周期均线**: 用于 `TREND_FILTER_TIMEFRAME` 上的移动平均线 (例如 `30`)。                                                                                                                     |
| `TREND_ADX_THRESHOLD_STRONG`       | **强趋势 ADX 阈值**: ADX 高于此值时视为强趋势，可能影响 ATR 乘数 (例如 `25`)。                                                                                                                     |
| `TREND_ADX_THRESHOLD_WEAK`         | **弱趋势 ADX 阈值**: ADX 低于此值时视为弱趋势，可能影响 ATR 乘数 (例如 `20`)。                                                                                                                     |
| `TREND_ATR_MULTIPLIER_STRONG`      | **强趋势 ATR 乘数**: 强趋势下，用于趋势判断动态阈值的 ATR 乘数。                                                                                                                                     |
| `TREND_ATR_MULTIPLIER_WEAK`        | **弱趋势 ATR 乘数**: 弱趋势下，用于趋势判断动态阈值的 ATR 乘数。                                                                                                                                     |
| `ENABLE_TREND_MEMORY`              | **启用趋势记忆**: `True` 允许趋势判断在信号短暂反向时保持一定 K 线数。                                                                                                                             |
| `TREND_CONFIRMATION_GRACE_PERIOD`  | **趋势记忆宽限期 (K线数)**: 在趋势记忆启用时，允许保持原趋势判断的 K 线数量。                                                                                                                        |
| `ENABLE_PULLBACK_QUALITY_FILTER`   | **启用回调质量过滤**: `True` 检查回调浪成交量是否过大。                                                                                                                                          |
| `PULLBACK_MAX_VOLUME_RATIO`        | **回调最大成交量比例**: 回调浪成交量与主升浪成交量的最大允许比例。                                                                                                                                   |
| `ENABLE_ENTRY_MOMENTUM_CONFIRMATION`| **启用入场动能确认**: `True` 使用 RSI 确认回调结束时动能是否恢复。                                                                                                                                  |
| `ENTRY_RSI_PERIOD`                 | **入场动能 RSI 周期**: 用于动能确认的 RSI 周期 (例如 `7`)。                                                                                                                                      |
| `ENTRY_RSI_CONFIRMATION_BARS`      | **入场动能确认 K 线数**: RSI 必须连续 N 根 K 线显示反转才能确认 (例如 `3`)。                                                                                                                         |
| **E. 算法 - 突破策略 (Settings)** |                                                                                                                                                                                                                  |
| `ENABLE_BREAKOUT_MODIFIER`         | **启用突破策略**: `True` 启用布林带挤压突破策略。                                                                                                                                                |
| `BREAKOUT_NOMINAL_VALUE_USDT`      | **突破策略名义价值 (USDT)**: 突破策略使用固定的名义价值开仓，**不使用**单笔风险百分比。                                                                                                                |
| `BREAKOUT_TIMEFRAME`               | **突破策略时间框架**: 执行突破策略的 K 线周期 (例如 `'3m'`)。                                                                                                                                     |
| `BREAKOUT_BBANDS_PERIOD`           | **突破策略布林带周期**: (例如 `20`)。                                                                                                                                                           |
| `BREAKOUT_BBANDS_STD_DEV`          | **突破策略布林带标准差**: (例如 `2.0`)。                                                                                                                                                        |
| `ENABLE_BBAND_SQUEEZE_FILTER`      | **启用布林带挤压过滤**: **[核心]** `True` 必须先发生“挤压”才允许触发突破信号。                                                                                                                      |
| `BBAND_SQUEEZE_LOOKBACK_PERIOD`    | **挤压回看周期 (K线数)**: 用于判断是否处于挤压状态的回看 K 线数量 (例如 `120`)。                                                                                                                      |
| `BBAND_SQUEEZE_THRESHOLD_PERCENTILE`| **挤压阈值分位数**: 当前布林带带宽必须低于过去 N 周期内此分位数才算挤压 (例如 `0.25` 代表 25%)。                                                                                                        |
| `BREAKOUT_GRACE_PERIOD_SECONDS`    | **突破冷却时间 (秒)**: 一次突破信号触发后的最短间隔时间。                                                                                                                                          |
| `BREAKOUT_VOLUME_CONFIRMATION`     | **启用突破成交量确认**: `True` 要求突破 K 线必须放量。                                                                                                                                            |
| `BREAKOUT_VOLUME_PERIOD`           | **突破成交量平均周期**: 用于计算平均成交量的 K 线数量。                                                                                                                                             |
| `BREAKOUT_VOLUME_MULTIPLIER`       | **突破成交量倍数**: 突破 K 线成交量必须大于平均成交量的此倍数。                                                                                                                                     |
| `BREAKOUT_RSI_CONFIRMATION`        | **启用突破 RSI 确认**: `True` 要求突破时 RSI 显示同向动能。                                                                                                                                        |
| `BREAKOUT_RSI_PERIOD`              | **突破 RSI 周期**: (例如 `14`)。                                                                                                                                                              |
| `BREAKOUT_RSI_THRESHOLD`           | **突破 RSI 阈值**: 多头突破要求 RSI > 阈值，空头突破要求 RSI < (100 - 阈值)。                                                                                                                        |
| `BREAKOUT_TRAIL_STOP_PERCENT`      | **突破追踪止损百分比**: **[专用]** 突破策略仓位使用此百分比进行追踪止损，而非 ATR。                                                                                                                   |
| **F. 算法 - 震荡策略 (Settings)** |                                                                                                                                                                                                                  |
| `ENABLE_RANGING_STRATEGY`          | **启用震荡策略**: `True` 启用均值回归策略。                                                                                                                                                      |
| `RANGING_TIMEFRAME`                | **震荡策略时间框架**: 执行震荡策略的 K 线周期 (例如 `'15m'`)。                                                                                                                                     |
| `RANGING_NOMINAL_VALUE_USDT`       | **震荡策略名义价值 (USDT)**: 震荡策略使用固定的名义价值开仓。                                                                                                                                       |
| `RANGING_ADX_THRESHOLD`            | **震荡 ADX 阈值**: ADX **低于**此值才被认为是适合震荡策略的行情。                                                                                                                                  |
| `RANGING_BBANDS_PERIOD`            | **震荡策略布林带周期**: (例如 `20`)。                                                                                                                                                           |
| `RANGING_BBANDS_STD_DEV`           | **震荡策略布林带标准差**: (例如 `2.0`)。                                                                                                                                                        |
| `RANGING_TAKE_PROFIT_TARGET`       | **震荡止盈目标**: 可选 `'middle'` (布林带中轨) 或 `'opposite'` (布林带对轨)。                                                                                                                     |
| `RANGING_STOP_LOSS_ATR_MULTIPLIER` | **震荡初始止损 ATR 倍数**: 用于计算震荡策略的初始止损距离。                                                                                                                                          |
| **G. 算法 - 仓位管理 (FuturesSettings)** |                                                                                                                                                                                                                  |
| `PYRAMIDING_ENABLED`               | **启用金字塔加仓**: `True` 允许在盈利的趋势/AI仓位上加仓。                                                                                                                                         |
| `PYRAMIDING_MAX_ADD_COUNT`         | **最大加仓次数**: (例如 `2`)。                                                                                                                                                                |
| `PYRAMIDING_ADD_SIZE_RATIO`        | **加仓尺寸比例**: 每次加仓的尺寸相对于上一次开仓尺寸的比例 (例如 `0.75`)。                                                                                                                            |
| `CHANDELIER_EXIT_ENABLED`          | **启用吊灯止损**: `True` 在盈利达到一定程度后切换到更激进的吊灯止损。                                                                                                                                 |
| `CHANDELIER_ACTIVATION_PROFIT_MULTIPLE` | **吊灯止损激活 R 倍数**: 利润达到 N 倍初始风险 (N 'R') 时激活吊灯止损 (例如 `2.0`)。                                                                                                              |
| `CHANDELIER_PERIOD`                | **吊灯止损回看周期**: 计算吊灯止损所用的 K 线回看周期 (例如 `16`)。                                                                                                                                   |
| `CHANDELIER_ATR_MULTIPLIER`        | **吊灯止损 ATR 倍数**: 吊灯止损从最高/最低点回撤的 ATR 倍数 (例如 `2.5`)。                                                                                                                           |
| `TRAILING_STOP_MIN_UPDATE_SECONDS` | **追踪止损最小更新间隔 (秒)**: 避免过于频繁地更新止损位 (例如 `60`)。                                                                                                                              |
| `ADAPTIVE_TRAILING_STOP_ENABLED`   | **启用自适应追踪止损**: `True` 允许追踪止损的 ATR 乘数根据市场波动率动态调整。                                                                                                                         |
| `TRAILING_STOP_ATR_SHORT_PERIOD`   | **自适应止损短周期 ATR**: (例如 `10`)。                                                                                                                                                        |
| `TRAILING_STOP_ATR_LONG_PERIOD`    | **自适应止损长周期 ATR**: (例如 `50`)。                                                                                                                                                        |
| `TRAILING_STOP_VOLATILITY_PAUSE_THRESHOLD` | **追踪止损波动暂停阈值**: 当市场波动极低 (ATR 相对于价格的比例低于此值) 时，暂停更新追踪止损。                                                                                                     |
| `TREND_EXIT_ADJUST_SL_ENABLED`     | **启用趋势反转防御止损**: `True` 在趋势信号与持仓方向不符时，触发防御性止损。                                                                                                                        |
| `TREND_EXIT_CONFIRMATION_COUNT`    | **趋势反转确认 K 线数**: 趋势信号需连续 N 根 K 线与持仓不符才触发防御止损 (例如 `3`)。                                                                                                                |
| `TREND_EXIT_ATR_MULTIPLIER`        | **防御性止损 ATR 倍数**: 触发防御性止损时使用的**更紧**的 ATR 倍数 (例如 `1.8`)。                                                                                                                   |
| `ENABLE_EXHAUSTION_ALERT`          | **启用趋势衰竭预警**: `True` 监控 ADX 下降，触发止损移至盈亏平衡。                                                                                                                                  |
| `EXHAUSTION_ADX_THRESHOLD`         | **衰竭 ADX 阈值**: ADX 必须曾高于此值。                                                                                                                                                         |
| `EXHAUSTION_ADX_FALLING_BARS`      | **衰竭 ADX 下降 K 线数**: ADX 需连续 N 根 K 线下降才触发预警 (例如 `3`)。                                                                                                                            |
| `ENABLE_REVERSAL_SIGNAL_ALERT`     | **启用危险反转 K 线预警**: `True` 监控可能导致止损的大幅反向 K 线。                                                                                                                                  |
| `REVERSAL_ALERT_BODY_ATR_MULTIPLIER`| **危险 K 线实体 ATR 倍数**: 反转 K 线实体需大于 N 倍 ATR (例如 `1.5`)。                                                                                                                            |
| `REVERSAL_ALERT_VOLUME_MULTIPLE`| **危险 K 线成交量倍数**: 反转 K 线成交量需大于 N 倍平均成交量 (例如 `2.0`)。                                                                                                                          |
