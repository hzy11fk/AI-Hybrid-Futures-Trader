import os
import logging
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pydantic import ConfigDict

# 加载 .env 文件中的环境变量
load_dotenv()

# ==============================================================================
# 全局设置 (Settings)
# ==============================================================================
# 这个类主要负责从.env文件加载密钥、设置全局开关等基础配置。

class Settings(BaseSettings):
    """全局应用程序设置"""
    # --- 模式与交易对配置 ---
    USE_TESTNET: bool = False
    FUTURES_SYMBOLS_LIST: list = ["BNB/USDT:USDT", "ETH/USDT:USDT"]
    FUTURES_INITIAL_PRINCIPAL: float = 100.0  # 请根据您投入的实际初始本金修改此值

    # --- API 密钥 (从 .env 文件读取) ---
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY: str = os.getenv("BINANCE_SECRET_KEY", "")
    BINANCE_TESTNET_API_KEY: str = os.getenv("BINANCE_TESTNET_API_KEY", "")
    BINANCE_TESTNET_SECRET_KEY: str = os.getenv("BINANCE_TESTNET_SECRET_KEY", "")

    # --- Bark 推送服务 ---
    BARK_URL_KEY: str = os.getenv("BARK_URL_KEY", "")

    # --- 趋势判断系统核心参数 ---
    TREND_SIGNAL_TIMEFRAME: str = '5m'
    TREND_FILTER_TIMEFRAME: str = '15m'
    TREND_SHORT_MA_PERIOD: int = 10
    TREND_LONG_MA_PERIOD: int = 30
    TREND_FILTER_MA_PERIOD: int = 50
    TREND_ADX_THRESHOLD_STRONG: int = 25
    TREND_ADX_THRESHOLD_WEAK: int = 20
    TREND_ATR_MULTIPLIER_STRONG: float = 1.5
    TREND_ATR_MULTIPLIER_WEAK: float = 0.7

    # --- 趋势判断增强参数 (成交量/动量/记忆) ---
    ENABLE_TREND_CONFIRMATION: bool = True
    ENABLE_TREND_MEMORY: bool = True
    TREND_CONFIRMATION_GRACE_PERIOD: int = 3
    TREND_VOLUME_CONFIRM_PERIOD: int = 20
    TREND_RSI_CONFIRM_PERIOD: int = 14
    TREND_RSI_UPPER_BOUND: int = 55
    TREND_RSI_LOWER_BOUND: int = 45

    # --- 动态成交量阈值参数 ---
    DYNAMIC_VOLUME_ENABLED: bool = True
    DYNAMIC_VOLUME_BASE_MULTIPLIER: float = 1.5
    DYNAMIC_VOLUME_ATR_PERIOD_SHORT: int = 10
    DYNAMIC_VOLUME_ATR_PERIOD_LONG: int = 50
    DYNAMIC_VOLUME_ADJUST_FACTOR: float = 0.5

    # --- 动量突破入场策略参数 (布林带) ---
    ENABLE_BREAKOUT_ENTRY: bool = True
    BREAKOUT_TIMEFRAME: str = '5m'
    BREAKOUT_BBANDS_PERIOD: int = 20
    BREAKOUT_BBANDS_STD_DEV: float = 2.0

    # --- 突发激增入场策略参数 ---
    ENABLE_SPIKE_ENTRY: bool = True
    SPIKE_TIMEFRAME: str = '5m'
    SPIKE_BODY_ATR_MULTIPLIER: float = 2.0
    SPIKE_VOLUME_MULTIPLIER: float = 2.5

    # --- 激增/突破信号触发的激进模式参数 ---
    SPIKE_ENTRY_GRACE_PERIOD_MINUTES: int = 10
    # -- 激进模式 (由布林带突破激活) --
    ENABLE_BREAKOUT_MODIFIER: bool = True
    BREAKOUT_GRACE_PERIOD_SECONDS: int = 180
    AGGRESSIVE_PULLBACK_ZONE_MULTIPLIER: float = 2.0
    AGGRESSIVE_RELAXED_VOLUME_MULTIPLIER: float = 0.8
    # -- 超级激进模式 (由波动/成交量激增激活) --
    ENABLE_SPIKE_MODIFIER: bool = True
    SPIKE_GRACE_PERIOD_SECONDS: int = 90
    SUPER_AGGRESSIVE_PULLBACK_ZONE_MULTIPLIER: float = 3.0
    SUPER_AGGRESSIVE_RELAXED_VOLUME_MULTIPLIER: float = 0.5
    AGGRESSIVE_ENTRY_SIZE_MULTIPLIER: float = 1.5

    # --- [核心新增] 资金费用同步配置 ---
    ENABLE_FUNDING_FEE_SYNC: bool = True       # 是否启用资金费用同步功能
    FUNDING_FEE_SYNC_INTERVAL_HOURS: int = 1   # 每隔多少小时同步一次资金费用流水

    # --- 策略表现反馈系统 (自适应参数) ---
    ENABLE_PERFORMANCE_FEEDBACK: bool = True
    PERFORMANCE_CHECK_INTERVAL_HOURS: int = 4
    MIN_TRADES_FOR_EVALUATION: int = 5  # 降低门槛以便更快启动
    PERF_WEIGHT_WIN_RATE: float = 0.40
    PERF_WEIGHT_PAYOFF_RATIO: float = 0.25
    PERF_WEIGHT_DRAWDOWN: float = 0.35

    # -- 动态参数的两极：激进型 vs 防御型 --
    AGGRESSIVE_PARAMS: dict = {
        "PULLBACK_ZONE_PERCENT": 0.2,
        "ATR_MULTIPLIER": 2.0,
        "PYRAMIDING_TRIGGER_PROFIT_MULTIPLE": 0.8
    }
    DEFENSIVE_PARAMS: dict = {
        "PULLBACK_ZONE_PERCENT": 0.6,
        "ATR_MULTIPLIER": 3.5,
        "PYRAMIDING_TRIGGER_PROFIT_MULTIPLE": 1.5
    }

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
        case_sensitive=True,
        extra='ignore'
    )

# ==============================================================================
# 合约策略专属设置 (FuturesSettings)
# ==============================================================================
# 这个类专门存放与合约交易行为直接相关的参数，与上面的全局/趋势判断参数分离。

class FuturesSettings:
    # --- 核心交易参数 ---
    FUTURES_LEVERAGE: int = 5
    FUTURES_MARGIN_MODE: str = 'isolated'
    FUTURES_RISK_PER_TRADE_PERCENT: float = 1.5  # 考虑到多币对和加仓，建议使用更保守的1%
    FUTURES_STOP_LOSS_PERCENT: float = 2.5
    
    # --- 入场与状态管理 ---
    FUTURES_ENTRY_PULLBACK_EMA_PERIOD: int = 10
    FUTURES_STATE_DIR: str = 'data'
    
    # --- 趋势不一致时的防御性止损 ---
    TREND_EXIT_ADJUST_SL_ENABLED: bool = True
    TREND_EXIT_CONFIRMATION_COUNT: int = 3
    TREND_EXIT_ATR_MULTIPLIER: float = 1.8
    
    # --- 金字塔加仓 ---
    PYRAMIDING_ENABLED: bool = True
    PYRAMIDING_MAX_ADD_COUNT: int = 2
    PYRAMIDING_ADD_SIZE_RATIO: float = 0.75
    # PYRAMIDING_TRIGGER_PROFIT_MULTIPLE 已被 Settings 中的动态参数取代，此处不再需要
    # ==============================================================================
    # --- [核心新增] 两阶段动态止损与吊灯止损 (Chandelier Exit) ---
    # ==============================================================================
    # 阶段一：使用 _update_trailing_stop 中的动态ATR参数 (dyn_atr_multiplier)
    # 阶段二：当盈利达到下面的倍数后，切换到更宽松的吊灯止损以捕捉大趋势
    
    CHANDELIER_EXIT_ENABLED: bool = True  # 是否启用两阶段止损系统
    
    # 从阶段一切换到阶段二的盈利门槛 (单位: R, 即初始风险的倍数)
    # 例如，设置为 2.0 意味着当浮动盈利达到初始风险的2倍时，自动切换到吊灯止损
    CHANDELIER_ACTIVATION_PROFIT_MULTIPLE: float = 2.0
    
    # 吊灯止损本身的参数
    CHANDELIER_PERIOD: int = 16  # 计算N周期最高/最低价的周期，22约等于一个月
    CHANDELIER_ATR_MULTIPLIER: float = 3.0 # ATR 乘数，3是常用值，越大越宽松
# --- 实例化配置 ---
settings = Settings()
futures_settings = FuturesSettings()
