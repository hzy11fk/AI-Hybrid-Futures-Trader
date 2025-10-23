# 异步多策略加密货币合约交易机器人

本项目是一个基于 Python `asyncio` 和 `ccxt` 库构建的高性能、多策略的加密货币合约（U本位）自动化交易机器人。它旨在通过结合趋势跟踪、突破和均值回归等多种交易逻辑，适应不同的市场环境，实现自动化交易决策和风险管理。

## ✨ 项目特色
- **多策略融合**: 集成了趋势回调、布林带突破和震荡均值回归三种核心交易策略，力求在不同市场条件下捕捉交易机会。
- **异步高性能**: 基于 `asyncio` 构建，能够高效地并发处理多个交易对的数据获取、策略计算和订单执行。
- **健壮的交易所交互**: 通过 `ExchangeClient` 封装 `ccxt` (包括 `ccxt.pro`)，实现了包括自动重试在内的健壮的网络通信机制。
- **精细化风险管理**:
    - 基于ATR动态计算初始止损。
    - 支持动态追踪止损 (Adaptive Trailing Stop Loss) 和 吊灯止损 (Chandelier Exit)。
    - 严格的单笔风险百分比控制。
    - 单笔最大保证金占用比例限制，防止单次重仓风险。
    - 当计算仓位超过保证金上限时，自动缩减仓位而不是取消交易。
- **高级仓位管理**:
    - 支持金字塔加仓 (Pyramiding)，在盈利时扩大优势。
    - 当计算加仓数量小于交易所最小要求时，自动调整为最小数量进行加仓。
    - 支持基于趋势分歧或衰竭信号的部分平仓 (Partial Take Profit)。
- **状态持久化**: 交易仓位状态和盈亏历史会被保存到本地文件 (`data` 目录)，确保程序重启后能恢复状态。
- **实时监控**: 内置Web服务器 (`web_server.py`)，提供可视化监控仪表盘，实时展示各交易对的K线图、持仓状态、浮动盈亏、策略信号和系统日志。
- **模拟测试**: 提供独立的纸上交易脚本 (`paper_trader.py`)，用于在不涉及真实资金的情况下，使用实时市场数据进行前瞻性测试 (Forward Testing)，并在测试结束后生成性能报告。
- **通知系统**: 集成 Bark App 通知，实时推送开仓、平仓、加仓、参数调整等关键事件（可在模拟测试中禁用）。

## 📈 交易策略简介
系统会根据对市场状态的判断 (`_detect_trend` 函数)，自动选择或倾向于执行以下策略之一：

### 趋势回调跟踪 (Trend Following - Pullback Entry) (`pullback_entry`)
- **核心思想**: 顺势而为，回调入场。
- **流程**:
    1. 通过比较5分钟快线EMA和慢线EMA，以及15分钟长期均线和ADX指标，判断主趋势方向 (上涨/下跌)。
    2. 等待价格回调至5分钟EMA形成的“价值区域”。
    3. 通过RSI指标确认动能是否在回调区恢复。
    4. 分析回调成交量是否萎缩，过滤掉潜在的反转风险。
    5. 满足所有条件后，以固定风险百分比计算仓位大小入场。
- **图标**: 📈

### 布林带突破 (Bollinger Band Breakout) (`breakout_momentum_trade`)
- **核心思想**: 捕捉由低波动转为高波动的趋势启动点。
- **流程**:
    1. 监控5分钟K线价格是否从布林带内部穿越到外部。
    2. **波动率过滤 (Squeeze Filter)**: 要求突破必须发生在布林带宽度处于历史低位（挤压状态）之后。
    3. **成交量确认**: 突破K线的成交量需显著放大。
    4. **RSI动量确认**: RSI值需支持突破方向。
    5. 满足所有条件后，以固定名义价值计算仓位大小入场。
- **图标**: ⚡️

### 震荡均值回归 (Mean Reversion - Ranging) (`ranging_entry`)
- **核心思想**: 在市场横盘震荡时，利用价格围绕价值中枢波动的特性，进行高抛低吸。
- **流程**:
    1. 首先确认市场处于震荡状态 (通过 `_detect_trend` 函数判断为 `sideways`)。
    2. 监控15分钟K线价格是否触及或穿过布林带上轨（做空）或下轨（做多）。
    3. 触及轨道后立即入场。
    4. 止损基于15分钟ATR动态设置。
    5. 止盈目标通常为布林带中轨。
    6. 以固定名义价值计算仓位大小入场。
- **图标**: ⚖️

## ⚙️ 功能描述
- **交易所客户端 (`exchange_client.py`)**: 封装了与交易所API的交互，增加了针对网络错误、超时等的自动重试逻辑，提高了稳定性。
- **主交易逻辑 (`futures_trader.py`)**: 包含了所有策略判断、订单执行、风险管理和仓位管理的核心代码。
- **仓位追踪器 (`position_tracker.py`)**: 负责精确记录每个交易对的持仓状态（方向、均价、数量、止損、止盈、加仓次数等），并将状态持久化到本地JSON文件。
- **利润追踪器 (`profit_tracker.py`)**: 记录每一笔已完成交易的详细信息（盈亏、手续费等），计算胜率、盈亏比等性能指标，并将历史记录持久化。
- **Web服务器 (`web_server.py`)**: 使用 `aiohttp` 构建，提供Web界面，通过API从运行中的交易员实例获取数据并展示。
- **模拟交易器 (`paper_trader.py`)**: 继承 `FuturesTrendTrader` 的策略逻辑，但重写了交易执行和初始化部分，将其导向一个内存中的模拟交易所 (`MockExchange`)，用于前瞻性测试。测试结束后会打印性能报告。
- **配置文件 (`config.py`)**: 使用 `pydantic-settings` 管理所有可配置参数，支持从 `.env` 文件加载敏感信息（如API密钥）。
- **辅助工具 (`helpers.py`)**: 包含日志设置、Bark通知发送、手续费计算等通用函数。
- **主入口 (`main2.py`)**: 负责初始化交易所连接、创建各交易对的 `FuturesTrendTrader` 实例、启动Web服务器和所有交易员的主循环。

## 🔧 配置项详解 (config.py)
配置文件分为 `Settings` (全局和通用策略设置) 和 `FuturesSettings` (合约交易特定设置) 两部分。

### `Settings` (全局与通用策略设置)

| 参数名 | 中文注释和功能说明 |
| :--- | :--- |
| **全局与环境设置** | |
| `USE_TESTNET` | **是否使用测试网**: `True` 使用币安测试网环境，`False` 使用实盘环境。 |
| `FUTURES_SYMBOLS_LIST` | **交易对列表**: 您希望机器人运行的合约列表。格式: `["BNB/USDT:USDT", "ETH/USDT:USDT"]`。 |
| `FUTURES_INITIAL_PRINCIPAL` | **初始本金**: 用于纸上交易 (`paper_trader.py`) 的起始资金，也用于Web UI计算总盈亏率。 |
| `BINANCE_API_KEY` | **API Key**: 您的币安主网 API Key。**强烈建议存储在 `.env` 文件中**。 |
| `BINANCE_SECRET_KEY` | **Secret Key**: 您的币安主网 Secret Key。**强烈建议存储在 `.env` 文件中**。 |
| `BINANCE_TESTNET_API_KEY` | **测试网 API Key**: 您的币安测试网 API Key。**强烈建议存储在 `.env` 文件中**。 |
| `BINANCE_TESTNET_SECRET_KEY` | **测试网 Secret Key**: 您的币安测试网 Secret Key。**强烈建议存储在 `.env` 文件中**。 |
| `BARK_URL_KEY` | **Bark 通知密钥**: 您的 Bark App 推送 URL。**强烈建议存储在 `.env` 文件中**。 |
| **主策略：趋势跟踪与回调** | |
| `TREND_SIGNAL_TIMEFRAME` | **信号时间周期**: 用于生成交易信号的K线周期，默认为 `'5m'`。 |
| `TREND_FILTER_TIMEFRAME` | **过滤时间周期**: 用于判断宏观大趋势的K线周期，默认为 `'15m'`。 |
| `TREND_SHORT_MA_PERIOD` | **短期EMA周期**: 在信号周期上计算的短期EMA均线周期，用于构成金叉/死叉信号。 |
| `TREND_LONG_MA_PERIOD` | **长期EMA周期**: 在信号周期上计算的长期EMA均线周期，回调的“价值区域”由快慢线构成。 |
| `TREND_FILTER_MA_PERIOD` | **宏观过滤均线周期**: 在过滤时间周期上计算的均线，用于确认大方向。 |
| `TREND_ADX_THRESHOLD_STRONG` | **强趋势ADX阈值**: ADX高于此值时，认为趋势强劲，会使用更积极的参数。 |
| `TREND_ADX_THRESHOLD_WEAK` | **弱趋势ADX阈值**: ADX低于此值时，认为趋势较弱，会使用更保守的参数。 |
| `ENABLE_PULLBACK_QUALITY_FILTER` | **启用回调质量过滤器**: `True` 会分析回调浪的成交量，若成交量过大则过滤信号。 |
| `PULLBACK_MAX_VOLUME_RATIO` | **回调成交量比例**: 若回调浪的平均成交量超过主升/跌浪的该比例，则认为回调力度过强，可能为反转。 |
| `ENABLE_ENTRY_MOMENTUM_CONFIRMATION` | **启用入场动能确认**: `True` 会在价格进入回调区后，使用RSI指标确认动能是否恢复。 |
| `ENTRY_RSI_PERIOD` | **RSI动能确认周期**: 用于动能确认的RSI指标计算周期。 |
| `ENTRY_RSI_CONFIRMATION_BARS` | **RSI动能确认K线数**: RSI需要连续多少根K线回升(做多)或回落(做空)才算确认信号。 |
| **子策略：波动率突破** | |
| `ENABLE_BREAKOUT_MODIFIER` | **启用突破策略**: `True` 激活基于布林带挤压后的突破策略。 |
| `BREAKOUT_NOMINAL_VALUE_USDT` | **突破策略开仓名义价值**: 突破策略采用固定名义价值（USDT）来计算开仓数量。 |
| `BREAKOUT_TIMEFRAME` | **突破策略时间周期**: 用于判断突破信号的K线周期，建议 `'3m'` 或 `'5m'`。 |
| `BREAKOUT_BBANDS_PERIOD` | **突破策略布林带周期**: 布林带指标的计算周期。 |
| `BREAKOUT_BBANDS_STD_DEV` | **突破策略布林带标准差**: 布林带的标准差倍数。 |
| `ENABLE_BBAND_SQUEEZE_FILTER` | **启用布林带挤压过滤器**: `True` 要求突破必须发生在布林带收缩（低波动）之后，是此策略的关键。 |
| `BBAND_SQUEEZE_LOOKBACK_PERIOD` | **挤压状态回看周期**: 用于判断当前布林带宽度是否处于历史低位的回看K线数量。 |
| `BBAND_SQUEEZE_THRESHOLD_PERCENTILE` | **挤压状态阈值百分位**: 布林带宽度小于过去N周期中的该百分位数时，视为“挤压”状态。 |
| `BREAKOUT_VOLUME_CONFIRMATION` | **启用突破成交量确认**: `True` 要求突破K线的成交量必须显著放大。 |
| `BREAKOUT_VOLUME_MULTIPLIER` | **突破成交量乘数**: 突破K线成交量需要超过过去平均成交量的倍数。 |
| `BREAKOUT_RSI_CONFIRMATION` | **启用突破RSI动量确认**: `True` 要求突破时的RSI值支持突破方向。 |
| `BREAKOUT_RSI_THRESHOLD`| **突破RSI阈值**: 多头突破时RSI需大于此值，空头突破时RSI需小于(100 - 此值)。 |
| `BREAKOUT_GRACE_PERIOD_SECONDS` | **突破信号冷却时间**: 两次突破信号之间需要的最小间隔时间（秒）。 |
| **子策略：震荡均值回归** | |
| `ENABLE_RANGING_STRATEGY`| **启用震荡策略**: `True` 在趋势判断为`sideways`时，激活基于布林带轨道的反向交易策略。 |
| `RANGING_TIMEFRAME` | **震荡策略时间周期**: 用于执行震荡策略的K线周期，建议使用较长周期如 `'15m'`。 |
| `RANGING_NOMINAL_VALUE_USDT`| **震荡策略开仓名义价值**: 震荡策略采用固定名义价值（USDT）来计算开仓数量。 |
| `RANGING_ADX_THRESHOLD` | **震荡ADX阈值**: 当ADX低于此值时，是激活震荡策略的条件之一。 |
| `RANGING_BBANDS_PERIOD` | **震荡策略布林带周期**: 震荡策略所用布林带的计算周期。 |
| `RANGING_BBANDS_STD_DEV` | **震荡策略布林带标准差**: 震荡策略所用布林带的标准差倍数。 |
| `RANGING_TAKE_PROFIT_TARGET` | **震荡策略止盈目标**: `'middle'` (中轨) 或 `'opposite'` (反向轨道)。 |
| `RANGING_STOP_LOSS_ATR_MULTIPLIER` | **震荡策略ATR止损乘数**: 止损距离为此ATR倍数。 |
| **实验性功能** | |
| `ENABLE_PERFORMANCE_FEEDBACK`| **启用性能反馈**: `True` 会根据历史交易表现（胜率、盈亏比等）在两套参数间动态切换 (实验性)。 |
| `AGGRESSIVE_PARAMS` | **激进参数集**: 机器人表现良好时采用的参数，通常止损更近、加仓更积极。 |
| `DEFENSIVE_PARAMS` | **保守参数集**: 机器人表现不佳时采用的参数，通常止损更远、加仓更保守。 |

### `FuturesSettings` (合约特定设置)

| 参数名 | 中文注释和功能说明 |
| :--- | :--- |
| **核心风控参数** | |
| `FUTURES_LEVERAGE` | **杠杆倍数**: 为您所有交易对设置的杠杆倍数。 |
| `FUTURES_MARGIN_MODE` | **保证金模式**: `'isolated'` (逐仓) 或 `'cross'` (全仓)。建议使用 `'isolated'` 以隔离风险。 |
| `FUTURES_RISK_PER_TRADE_PERCENT` | **单笔风险百分比**: （仅用于趋势策略）根据此百分比和止损距离计算开仓大小，是核心风控。 |
| `MAX_MARGIN_PER_TRADE_RATIO` | **最大保证金占用率**: **非常重要的风控参数**。单笔开仓所需保证金不得超过总权益的此比例，防止单次重仓。 |
| `MIN_NOMINAL_VALUE_USDT` | **最小名义价值**: 交易所允许的最小开仓价值（USDT）。如果计算出的仓位价值低于此值，会自动调整。 |
| `USE_ATR_FOR_INITIAL_STOP` | **使用ATR计算初始止损**: `True` 使用ATR动态计算初始止损，`False` 使用固定百分比。 |
| `INITIAL_STOP_ATR_MULTIPLIER` | **初始止损ATR乘数**: 初始止损距离 = ATR * 此乘数。 |
| `FUTURES_STATE_DIR` | **状态文件目录**: 用于存储仓位和利润历史记录的文件夹名称。 |
| **仓位管理与止损策略** | |
| `PYRAMIDING_ENABLED` | **启用金字塔加仓**: `True` 允许在盈利的趋势头寸上进行加仓。 |
| `PYRAMIDING_MAX_ADD_COUNT` | **最大加仓次数**: 一笔初始订单最多允许加仓的次数。 |
| `PYRAMIDING_ADD_SIZE_RATIO`| **加仓大小比例**: 每次加仓的数量是上一笔开仓/加仓数量的此比例。 |
| `TREND_EXIT_ADJUST_SL_ENABLED`| **启用趋势分歧止损调整**: 当短期趋势与宏观趋势不一致时，是否收紧止损。 |
| `TREND_EXIT_CONFIRMATION_COUNT`| **趋势分歧确认K线数**: 需要连续多少根K线出现趋势分歧，才触发止损调整。 |
| `TREND_EXIT_ATR_MULTIPLIER`| **趋势分歧止损ATR乘数**: 触发分歧时，将止损调整至 `现价 +/- ATR * 此乘数` 的位置。 |
| `ADAPTIVE_TRAILING_STOP_ENABLED`| **启用自适应追踪止损**: `True` 会根据短期和长期波动率的比值，动态调整追踪止损的ATR乘数。 |
| `TRAILING_STOP_MIN_UPDATE_SECONDS`| **追踪止损最小更新间隔**: 止损位更新的最小时间间隔（秒），防止过于频繁的更新。 |
| `CHANDELIER_EXIT_ENABLED`| **启用吊灯止损**: `True` 在盈利达到一定程度后，切换到更灵敏的吊灯止损模式以保护利润。 |
| `CHANDELIER_ACTIVATION_PROFIT_MULTIPLE`| **吊灯止损激活盈利倍数**: 当浮动盈利达到 `初始风险(R) * 此倍数` 时，从普通ATR追损切换为吊灯止损。 |
| `CHANDELIER_PERIOD` | **吊灯止损计算周期**: 用于计算吊灯止损的高点/低点的回看周期。 |
| `CHANDELIER_ATR_MULTIPLIER`| **吊灯止损ATR乘数**: 吊灯止损的回撤距离 = ATR * 此乘数。 |
| `ENABLE_EXHAUSTION_ALERT`| **启用趋势衰竭预警**: `True` 会监控ADX是否持续下降，若成立则提前将止损移动到保本位置。 |
| `EXHAUSTION_ADX_FALLING_BARS`| **ADX连续下降K线数**: ADX需要连续多少根K线回落才视为衰竭信号。 |
| `ENABLE_REVERSAL_SIGNAL_ALERT` | **启用危险反转信号预警**: `True` 会监控是否存在成交量和实体都很大的反向K线，若存在则立即收紧止损。 |
| `REVERSAL_ALERT_BODY_ATR_MULTIPLIER` | **反转K线实体ATR乘数**: 反向K线实体需超过此倍数的ATR。 |
| `REVERSAL_ALERT_VOLUME_MULTIPLE`| **反转K线成交量乘数**: 反向K线成交量需超过此倍数的平均成交量。 |



## 🚀 如何使用
### 1. 环境准备
- Python 3.10 或更高版本。
# 克隆项目
  ```bash
- git clone https://github.com/hzy11fk/Adaptive-Trend-Bot.git
- cd Adaptive-Trend-Bot
   ```
# 创建虚拟环境
  ```bash
python -m venv .venv
   ```
# 激活虚拟环境
# Windows:
  ```bash
.\.venv\Scripts\activate
   ```
# Linux/Mac:
  ```bash
source .venv/bin/activate
   ```
- 安装所需的库:
  ```bash
  pip install ccxt pandas numpy python-dotenv pydantic pydantic-settings aiohttp requests
  ```
### 2. 配置
1.  **创建 `.env` 文件**: 在项目根目录下创建一个名为 `.env` 的文本文件。
2.  **填入密钥**: 在 `.env` 文件中，按以下格式填入您的API密钥和Bark密钥：
    ```dotenv
    BINANCE_API_KEY=YOUR_MAINNET_API_KEY
    BINANCE_SECRET_KEY=YOUR_MAINNET_SECRET_KEY
    BINANCE_TESTNET_API_KEY=YOUR_TESTNET_API_KEY
    BINANCE_TESTNET_SECRET_KEY=YOUR_TESTNET_SECRET_KEY
    BARK_URL_KEY=YOUR_BARK_PUSH_URL_KEY
    ```
    如果您只想使用主网，测试网密钥可以留空，反之亦然。
    确保从币安官网获取API Key时，勾选了“允许合约”权限。



3.  **调整 `config.py`**: 根据您的交易偏好和风险承受能力，仔细检查并调整 `config.py` 文件中的各个参数值。强烈建议在实盘前，先使用测试网和模拟交易进行充分验证。

### 3. 运行实盘交易
1.  **确认配置**: 确保 `config.py` 中的 `USE_TESTNET = False`。
2.  **启动程序**: 在终端中运行主入口文件：
    ```bash
    python main.py
    ```
    程序启动后，会初始化交易所连接，为每个配置的交易对创建交易员实例，然后启动Web服务器和所有交易员的主循环。
3.  **监控**: 打开浏览器，访问 `http://<您的服务器IP>:58182` (默认端口，可在 `web_server.py` 中修改) 即可看到实时监控界面。同时，您的Bark App也会收到交易通知。
4.  **停止**: 在运行程序的终端中按下 `Ctrl + C` 可以优雅地停止程序。

### 4. 运行前瞻性测试 (模拟交易)
1.  **确认配置**: 您可以选择在主网 (`USE_TESTNET = False`) 或测试网 (`USE_TESTNET = True`) 的实时数据上进行模拟。强烈推荐创建一个只有“允许读取”权限的API Key 用于模拟测试，以确保100%安全。
2.  **启动脚本**: 在终端中运行模拟交易脚本：
    ```bash
    python paper_trader.py
    ```
