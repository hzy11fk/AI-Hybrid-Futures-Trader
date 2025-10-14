# 文件: futures_trader.py (最终修正版 - 修复ROUND_UP调用)

import logging
import asyncio
import time
import numpy as np
import pandas as pd
import ccxt # <--- [核心修正] 导入 ccxt 库本身
from ccxt.base.errors import ExchangeError, NetworkError, InsufficientFunds
from config import futures_settings, settings
from position_tracker import PositionTracker
from helpers import send_bark_notification
from profit_tracker import ProfitTracker # <--- [新增] 导入新的利润跟踪器
from enum import Enum
class Trend(Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"

class FuturesTrendTrader:
    
    def __init__(self, exchange, symbol: str):
        self.exchange = exchange
        self.symbol = symbol
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.symbol}]")
        self.position = PositionTracker(self.symbol, state_dir=futures_settings.FUTURES_STATE_DIR)
        self.initialized = False
        self.last_status_log_time = 0
        self.last_trend_analysis = {}
        self.last_spike_analysis = {}
        
        self.profit_tracker = ProfitTracker(
            state_dir=futures_settings.FUTURES_STATE_DIR, 
            symbol=self.symbol,
            initial_principal=settings.FUTURES_INITIAL_PRINCIPAL
        )
        
        self.trend_exit_counter = 0
        self.trend_confirmed_state = 'sideways'
        self.trend_grace_period_counter = 0
        self.trend_confirmation_timestamp = 0
        # --- [核心重构] 统一的激进模式状态管理 ---
        self.aggressive_mode_until = 0  # 激进模式的截止时间戳
        self.aggression_level = 0       # 激进等级: 0=常规, 1=激进(突破), 2=超级激进(激增)
        # --- [核心新增] 资金费用同步的计时器 ---
        self.last_funding_check_time = 0

        self.last_perf_check_time = 0
        self.dyn_pullback_zone_percent = (settings.AGGRESSIVE_PARAMS['PULLBACK_ZONE_PERCENT'] + settings.DEFENSIVE_PARAMS['PULLBACK_ZONE_PERCENT']) / 2
        self.dyn_atr_multiplier = (settings.AGGRESSIVE_PARAMS['ATR_MULTIPLIER'] + settings.DEFENSIVE_PARAMS['ATR_MULTIPLIER']) / 2
        self.dyn_pyramiding_trigger = (settings.AGGRESSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE'] + settings.DEFENSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE']) / 2


    async def _sync_funding_fees(self):
        """[修正] 定期同步交易所的资金费用流水，使用币安特定的API方法和正确的symbol格式"""
        if not settings.ENABLE_FUNDING_FEE_SYNC:
            return

        current_time = time.time()
        if current_time - self.last_funding_check_time < settings.FUNDING_FEE_SYNC_INTERVAL_HOURS * 3600:
            return

        self.logger.info("开始同步资金费用流水...")
        try:
            last_ts = self.profit_tracker.last_funding_fee_timestamp
            since = last_ts + 1 if last_ts > 0 else None

            # --- [核心修正] ---
            # 1. 从ccxt获取币安API所需的原生symbol格式 (例如, 'BNB/USDT:USDT' -> 'BNBUSDT')
            market = self.exchange.exchange.market(self.symbol)
            binance_native_symbol = market['id']

            # 2. 准备API所需的参数
            params = {
                'symbol': binance_native_symbol, # 使用原生格式的symbol
                'incomeType': 'FUNDING_FEE'
            }
            if since:
                params['startTime'] = since

            # 3. 使用币安U本位合约专用的隐式方法 fapiPrivateGetIncome
            income_history = await self.exchange.exchange.fapiPrivateGetIncome(params)
            # --- 修正结束 ---

            if income_history:
                self.profit_tracker.add_funding_fees(income_history)
            else:
                self.logger.info("未发现新的资金费用记录。")

            self.last_funding_check_time = current_time
        except Exception as e:
            self.logger.error(f"同步资金费用时发生错误: {e}", exc_info=True)


    async def initialize(self):
        """初始化，并根据需要从历史数据自动创建利润账本"""
        try:
            await self.exchange.load_markets()
            
            # --- [核心修改] 检查利润账本是否为全新，如果是，则从历史初始化 ---
            if self.profit_tracker.is_new:
                await self._initialize_profit_from_history()
            
            self.logger.info(f"正在为 {self.symbol} 设置杠杆为 {futures_settings.FUTURES_LEVERAGE}x...")
            await self.exchange.set_leverage(futures_settings.FUTURES_LEVERAGE, self.symbol)
            self.logger.info(f"正在为 {self.symbol} 设置保证金模式为 {futures_settings.FUTURES_MARGIN_MODE}...")
            await self.exchange.set_margin_mode(futures_settings.FUTURES_MARGIN_MODE, self.symbol)
            self.logger.info(f"合约趋势策略初始化成功: {self.symbol}")
            self.initialized = True
        except ExchangeError as e:
            self.logger.warning(f"设置杠杆或保证金模式可能失败 (请手动确认): {e}")
            self.initialized = True
        except Exception as e:
            self.logger.error(f"初始化失败: {e}", exc_info=True)
            self.initialized = False
    async def get_bollinger_bands_data(self):
        """[新增] 专门用于计算并返回最新的布林带上、中、下轨值"""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                self.symbol, 
                timeframe=settings.BREAKOUT_TIMEFRAME, 
                limit=settings.BREAKOUT_BBANDS_PERIOD + 5
            )
            if not ohlcv or len(ohlcv) < settings.BREAKOUT_BBANDS_PERIOD:
                return None

            closes = pd.Series([c[4] for c in ohlcv])

            middle_band = closes.rolling(window=settings.BREAKOUT_BBANDS_PERIOD).mean()
            std_dev = closes.rolling(window=settings.BREAKOUT_BBANDS_PERIOD).std()
            upper_band = middle_band + (std_dev * settings.BREAKOUT_BBANDS_STD_DEV)
            lower_band = middle_band - (std_dev * settings.BREAKOUT_BBANDS_STD_DEV)

            # 返回最后一根完整K线的布林带值
            return {
                "upper": upper_band.iloc[-2],
                "middle": middle_band.iloc[-2],
                "lower": lower_band.iloc[-2]
            }
        except Exception as e:
            self.logger.error(f"计算布林带数据时出错: {e}", exc_info=True)
            return None
    async def _initialize_profit_from_history(self):
        """【V3 最终手续费修正版】稳健地处理可能为None的fee对象。"""
        self.logger.warning("利润账本文件不存在，正在尝试从交易所历史成交记录中自动初始化...")
        try:
            trades = await self.exchange.fetch_my_trades(self.symbol, limit=1000)
            if not trades:
                self.logger.info("未在交易所找到任何历史成交记录，利润账本将从 0 开始。")
                self.profit_tracker.initialize_profit(0.0)
                return

            trades.sort(key=lambda x: x['timestamp'])
            from collections import deque
            buy_queue = deque([t for t in trades if t['side'] == 'buy'])
            sell_list = [t for t in trades if t['side'] == 'sell']
            total_pnl = 0.0
            trades_pnl_list = []

            for sell_trade in sell_list:
                # --- [核心修正] 使用更安全的方式获取手续费 ---
                sell_fee_info = sell_trade.get('fee')
                sell_fee = sell_fee_info.get('cost', 0.0) if sell_fee_info else 0.0

                sell_amount_to_match = sell_trade['amount']
                while sell_amount_to_match > 1e-9 and buy_queue:
                    buy_trade = buy_queue[0]
                    buy_fee_info = buy_trade.get('fee')
                    buy_fee = buy_fee_info.get('cost', 0.0) if buy_fee_info else 0.0

                    matched_amount = min(sell_amount_to_match, buy_trade['amount'])
                    gross_pnl = (sell_trade['price'] - buy_trade['price']) * matched_amount

                    sell_fee_for_match = (sell_fee / sell_trade['amount']) * matched_amount if sell_trade['amount'] > 0 else 0
                    original_buy_amount = next((t['amount'] for t in trades if t['id'] == buy_trade['id']), buy_trade['amount'])
                    buy_fee_for_match = (buy_fee / original_buy_amount) * matched_amount if original_buy_amount > 0 else 0

                    net_pnl = gross_pnl - sell_fee_for_match - buy_fee_for_match
                    total_pnl += net_pnl
                    trades_pnl_list.append(net_pnl)

                    sell_amount_to_match -= matched_amount
                    buy_trade['amount'] -= matched_amount

                    if buy_trade['amount'] < 1e-9:
                        buy_queue.popleft()

            self.logger.info(f"历史成交记录分析完成，计算出的累计净利润为: {total_pnl:.2f} USDT")
            self.profit_tracker.initialize_profit(total_pnl, trades_pnl_list)
        except Exception as e:
            self.logger.error(f"从历史成交记录初始化利润账本时发生严重错误: {e}", exc_info=True)
            self.logger.warning("由于初始化失败，利润账本将从 0 开始。")
            self.profit_tracker.initialize_profit(0.0, [])
    async def _update_dynamic_parameters(self):
        """根據策略表現得分，動態調整交易參數。"""
        if not settings.ENABLE_PERFORMANCE_FEEDBACK:
            return

        score = self.profit_tracker.get_performance_score()
        if score is None:
            self.logger.info("交易历史不足，暂不进行动态参数调整。")
            return

        self.logger.info(f"策略综合表现得分: {score:.3f}，开始调整动态参数...")

        # 線性插值函數
        def interpolate(agg_val, def_val, s):
            return def_val + (agg_val - def_val) * s

        # 計算新的動態參數
        self.dyn_pullback_zone_percent = interpolate(settings.AGGRESSIVE_PARAMS['PULLBACK_ZONE_PERCENT'], settings.DEFENSIVE_PARAMS['PULLBACK_ZONE_PERCENT'], score)
        self.dyn_atr_multiplier = interpolate(settings.AGGRESSIVE_PARAMS['ATR_MULTIPLIER'], settings.DEFENSIVE_PARAMS['ATR_MULTIPLIER'], score)
        self.dyn_pyramiding_trigger = interpolate(settings.AGGRESSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE'], settings.DEFENSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE'], score)

        log_msg = (
            f"动态参数已更新 (得分: {score:.3f}):\n"
            f"  - 回调区参数: {self.dyn_pullback_zone_percent:.2f}%\n"
            f"  - ATR止损参数: {self.dyn_atr_multiplier:.2f}\n"
            f"  - 加仓触发倍数: {self.dyn_pyramiding_trigger:.2f}"
        )
        self.logger.warning(log_msg)
        send_bark_notification(log_msg, f"⚙️ {self.symbol} 策略参数自适应调整")
    async def get_adx_data(self, period=14, ohlcv_df: pd.DataFrame = None):
        """使用EMA平滑计算ADX (可接收外部数据)"""
        try:
            if ohlcv_df is None:
                limit = period * 10
                ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe='15m', limit=limit)
                if not ohlcv or len(ohlcv) < period * 2:
                    self.logger.warning("ADX计算所需K线数据不足")
                    return None
                ohlcv_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            highs, lows, closes = ohlcv_df['high'].to_numpy(), ohlcv_df['low'].to_numpy(), ohlcv_df['close'].to_numpy()
            plus_dm_list, minus_dm_list, tr_list = [], [], []
            for i in range(1, len(highs)):
                move_up, move_down = highs[i] - highs[i-1], lows[i-1] - lows[i]
                plus_dm = move_up if move_up > move_down and move_up > 0 else 0
                minus_dm = move_down if move_down > move_up and move_down > 0 else 0
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                plus_dm_list.append(plus_dm); minus_dm_list.append(minus_dm); tr_list.append(tr)
            span = 2 * period - 1
            smooth_plus_dm, smooth_minus_dm, smooth_tr = pd.Series(plus_dm_list).ewm(span=span, adjust=False).mean(), pd.Series(minus_dm_list).ewm(span=span, adjust=False).mean(), pd.Series(tr_list).ewm(span=span, adjust=False).mean()
            plus_di, minus_di = (smooth_plus_dm / smooth_tr) * 100, (smooth_minus_dm / smooth_tr) * 100
            dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1)) * 100
            return dx.ewm(span=span, adjust=False).mean().iloc[-1]
        except Exception as e:
            self.logger.error(f"计算ADX失败: {e}"); return None



    async def _detect_trend(self, ohlcv_5m: list = None, ohlcv_15m: list = None):
        """
        [V2 - 修正宽限期逻辑] 双周期共振趋势判断。
        宽限期 (Grace Period) 现在以K线为单位消耗，而不是循环次数。
        """
        try:
            # --- 第一部分：数据获取和初步价格趋势判断 ---
            if ohlcv_5m is None or ohlcv_15m is None:
                signal_tf, filter_tf = settings.TREND_SIGNAL_TIMEFRAME, settings.TREND_FILTER_TIMEFRAME
                ohlcv_limit = max(settings.TREND_LONG_MA_PERIOD, settings.TREND_VOLUME_CONFIRM_PERIOD, settings.TREND_RSI_CONFIRM_PERIOD, settings.DYNAMIC_VOLUME_ATR_PERIOD_LONG) + 5
                self.logger.debug("_detect_trend 正在独立获取K线数据...")
                ohlcv_5m, ohlcv_15m = await asyncio.gather(
                    self.exchange.fetch_ohlcv(self.symbol, timeframe=signal_tf, limit=ohlcv_limit),
                    self.exchange.fetch_ohlcv(self.symbol, timeframe=filter_tf, limit=settings.TREND_FILTER_MA_PERIOD + 50)
                )

            if not all([ohlcv_5m, ohlcv_15m]):
                return 'sideways'
            
            # [新增] 获取当前正在形成的K线的时间戳
            current_kline_timestamp = ohlcv_5m[-1][0] if ohlcv_5m else 0

            signal_df = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            signal_closes = signal_df['close'].to_numpy()
            
            current_price = signal_closes[-1]
            short_ma = np.mean(signal_closes[-settings.TREND_SHORT_MA_PERIOD:])
            long_ma = np.mean(signal_closes[-settings.TREND_LONG_MA_PERIOD:])
            if long_ma == 0: return 'sideways'
            diff_ratio = (short_ma - long_ma) / long_ma
            
            ohlcv_15m_df = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            adx_value = await self.get_adx_data(period=14, ohlcv_df=ohlcv_15m_df)
            
            high_low = signal_df['high'] - signal_df['low']
            high_close = np.abs(signal_df['high'] - signal_df['close'].shift())
            low_close = np.abs(signal_df['low'] - signal_df['close'].shift())
            tr = np.max(pd.concat([high_low, high_close, low_close], axis=1), axis=1)
            atr_value = tr.ewm(span=14, adjust=False).mean().iloc[-1]
            
            if adx_value is None: ATR_MULTIPLIER = 1.0
            elif adx_value > settings.TREND_ADX_THRESHOLD_STRONG: ATR_MULTIPLIER = settings.TREND_ATR_MULTIPLIER_STRONG
            elif adx_value < settings.TREND_ADX_THRESHOLD_WEAK: ATR_MULTIPLIER = settings.TREND_ATR_MULTIPLIER_WEAK
            else: ATR_MULTIPLIER = 1.0
            
            dynamic_threshold = (atr_value / current_price) * ATR_MULTIPLIER if current_price > 0 else 0
            
            self.logger.info(
                f"[{self.symbol}] 5m信号判断: "
                f"均线差值比率={diff_ratio:.6f}, "
                f"动态阈值=±{dynamic_threshold:.6f}, "
                f"ATR={atr_value:.4f}, "
                f"乘数={ATR_MULTIPLIER:.2f}"
            )

            signal_trend = 'sideways'
            if diff_ratio > dynamic_threshold: signal_trend = 'uptrend'
            elif diff_ratio < -dynamic_threshold: signal_trend = 'downtrend'

            filter_closes = ohlcv_15m_df['close'].to_numpy()
            filter_ma = np.mean(filter_closes[-settings.TREND_FILTER_MA_PERIOD:])
            filter_env = 'bullish' if current_price > filter_ma else 'bearish'
            
            price_trend_result = 'sideways'
            if signal_trend == 'uptrend' and filter_env == 'bullish': price_trend_result = 'uptrend'
            elif signal_trend == 'downtrend' and filter_env == 'bearish': price_trend_result = 'downtrend'

            # --- 第二部分：趋势记忆(宽限期)逻辑 ---
            if settings.ENABLE_TREND_MEMORY:
                # [修改] 熔断机制：如果基础趋势变化，立即终止宽限期
                if price_trend_result != self.trend_confirmed_state or self.trend_confirmed_state == 'sideways':
                    self.trend_grace_period_counter = 0
                
                # [修改] 宽限期生效逻辑
                elif self.trend_grace_period_counter > 0:
                    # [核心修改] 只有在新的一根K线出现时，才消耗计数器
                    if current_kline_timestamp > self.trend_confirmation_timestamp:
                        self.trend_grace_period_counter -= 1
                        self.trend_confirmation_timestamp = current_kline_timestamp # 更新时间戳
                        self.logger.info(f"新K线形成，宽限期剩余: {self.trend_grace_period_counter}根K线。")

                    self.logger.debug(f"趋势记忆生效: 维持 [{self.trend_confirmed_state.upper()}] 判断。")
                    
                    self.last_trend_analysis = {
                        "signal_trend": signal_trend, "filter_env": filter_env, "confirmation": f"In Grace({self.trend_grace_period_counter})",
                        "diff_ratio": diff_ratio, "dynamic_threshold": dynamic_threshold, "adx_value": adx_value,
                        "current_volume": None, "vma": None, "rsi": None, "volume_multiplier": None
                    }
                    return self.trend_confirmed_state
            
            # --- 第三部分：严格确认逻辑 ---
            self.last_trend_analysis = {
                "diff_ratio": diff_ratio, "dynamic_threshold": dynamic_threshold, "adx_value": adx_value,
                "signal_trend": signal_trend, "filter_env": filter_env, "current_volume": None, "vma": None,
                "rsi": None, "confirmation": "N/A", "volume_multiplier": None
            }

            if not self.position.is_position_open():
                if price_trend_result == 'sideways':
                    return 'sideways'
                
                confirmation_passed = True
                is_breakout_grace_period = time.time() < self.aggressive_mode_until
                
                if is_breakout_grace_period:
                    if self.aggression_level == 2:
                        volume_multiplier = settings.SUPER_AGGRESSIVE_RELAXED_VOLUME_MULTIPLIER
                    else:
                        volume_multiplier = settings.AGGRESSIVE_RELAXED_VOLUME_MULTIPLIER
                elif settings.DYNAMIC_VOLUME_ENABLED:
                    short_atr = tr.ewm(span=settings.DYNAMIC_VOLUME_ATR_PERIOD_SHORT, adjust=False).mean().iloc[-2]
                    long_atr = tr.ewm(span=settings.DYNAMIC_VOLUME_ATR_PERIOD_LONG, adjust=False).mean().iloc[-2]
                    if long_atr > 0:
                        volatility_ratio = short_atr / long_atr
                        adjustment = (volatility_ratio - 1) * settings.DYNAMIC_VOLUME_ADJUST_FACTOR
                        volume_multiplier = settings.DYNAMIC_VOLUME_BASE_MULTIPLIER + adjustment
                        volume_multiplier = max(1.1, min(2.5, volume_multiplier))
                else:
                    volume_multiplier = settings.DYNAMIC_VOLUME_BASE_MULTIPLIER

                self.last_trend_analysis['volume_multiplier'] = volume_multiplier

                signal_volumes = signal_df['volume'].to_numpy()
                if len(signal_volumes) < settings.TREND_VOLUME_CONFIRM_PERIOD + 2:
                    self.logger.warning("成交量数据不足，跳过确认。")
                else:
                    last_closed_volume = signal_volumes[-2]
                    vma = np.mean(signal_volumes[-settings.TREND_VOLUME_CONFIRM_PERIOD-2:-2])
                    self.last_trend_analysis['current_volume'] = last_closed_volume
                    self.last_trend_analysis['vma'] = vma
                    if last_closed_volume < vma * volume_multiplier:
                        confirmation_passed = False
                        self.last_trend_analysis["confirmation"] = "Volume Failed"
                
                if confirmation_passed:
                    if len(signal_closes) < settings.TREND_RSI_CONFIRM_PERIOD + 1:
                        self.logger.warning("RSI数据不足，跳过确认。")
                    else:
                        delta = np.diff(signal_closes)
                        gain, loss = np.where(delta > 0, delta, 0), np.where(delta < 0, -delta, 0)
                        avg_gain = pd.Series(gain).ewm(alpha=1/settings.TREND_RSI_CONFIRM_PERIOD, adjust=False).mean()
                        avg_loss = pd.Series(loss).ewm(alpha=1/settings.TREND_RSI_CONFIRM_PERIOD, adjust=False).mean()
                        rs = avg_gain.iloc[-1] / avg_loss.iloc[-1] if avg_loss.iloc[-1] != 0 else np.inf
                        rsi = 100 - (100 / (1 + rs))
                        self.last_trend_analysis['rsi'] = rsi
                        if (price_trend_result == 'uptrend' and rsi < settings.TREND_RSI_UPPER_BOUND) or \
                           (price_trend_result == 'downtrend' and rsi > settings.TREND_RSI_LOWER_BOUND):
                            confirmation_passed = False
                            self.last_trend_analysis["confirmation"] = "RSI Failed"

                if confirmation_passed:
                    self.logger.info(f"趋势信号 [{price_trend_result.upper()}] 通过严格确认！启动趋势记忆。")
                    self.last_trend_analysis["confirmation"] = "Passed"
                    if settings.ENABLE_TREND_MEMORY:
                        self.trend_confirmed_state = price_trend_result
                        self.trend_grace_period_counter = settings.TREND_CONFIRMATION_GRACE_PERIOD
                        # [新增] 记录确认时的时间戳
                        self.trend_confirmation_timestamp = current_kline_timestamp
                    return price_trend_result
                else:
                    self.logger.info(f"趋势信号 [{price_trend_result.upper()}] 未通过严格确认 ({self.last_trend_analysis.get('confirmation', 'N/A')})。")
                    if settings.ENABLE_TREND_MEMORY:
                        self.trend_confirmed_state = 'sideways'
                    return 'sideways'
            
            self.last_trend_analysis['confirmation'] = 'N/A (In Position)'
            return price_trend_result

        except Exception as e:
            self.logger.error(f"趋势过滤器 _detect_trend 发生严重错误: {e}", exc_info=True)
            return 'sideways'

    async def _check_spike_entry_signal(self):
        """[修改] 不再直接入场，而是作为信号发射器，激活“超级激进”模式"""
        self.last_spike_analysis = {"status": "Monitoring", "current_body": None, "body_threshold": None, "current_volume": None, "volume_threshold": None}
        if not settings.ENABLE_SPIKE_MODIFIER or self.position.is_position_open():
            self.last_spike_analysis["status"] = "Disabled or In Position"
            return

        try:
            ohlcv_limit = max(settings.TREND_VOLUME_CONFIRM_PERIOD, 14, settings.TREND_FILTER_MA_PERIOD) + 5
            ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.SPIKE_TIMEFRAME, limit=ohlcv_limit)
            
            if not ohlcv or len(ohlcv) < ohlcv_limit - 2:
                self.last_spike_analysis["status"] = "Not enough data"
                return

            current_candle = ohlcv[-1]
            current_open, _, _, current_close, current_volume = current_candle[1], current_candle[2], current_candle[3], current_candle[4], current_candle[5]
            
            atr = await self.get_atr_data(period=14)
            if atr is None: return
            
            current_body_size = abs(current_close - current_open)
            body_threshold = atr * settings.SPIKE_BODY_ATR_MULTIPLIER
            self.last_spike_analysis.update({"current_body": current_body_size, "body_threshold": body_threshold})
            
            if current_body_size < body_threshold:
                self.last_spike_analysis["status"] = "Body too small"
                return
            
            volumes = np.array([c[5] for c in ohlcv])
            vma = np.mean(volumes[-settings.TREND_VOLUME_CONFIRM_PERIOD-1:-1])
            volume_threshold = vma * settings.SPIKE_VOLUME_MULTIPLIER
            self.last_spike_analysis.update({"current_volume": current_volume, "volume_threshold": volume_threshold})

            if current_volume < volume_threshold:
                self.last_spike_analysis["status"] = "Volume too low"
                return
            
            filter_ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_FILTER_TIMEFRAME, limit=settings.TREND_FILTER_MA_PERIOD + 2)
            if not filter_ohlcv or len(filter_ohlcv) < settings.TREND_FILTER_MA_PERIOD: return
            
            filter_closes = np.array([c[4] for c in filter_ohlcv])
            filter_ma = np.mean(filter_closes[-settings.TREND_FILTER_MA_PERIOD:])
            filter_env = 'bullish' if current_close > filter_ma else 'bearish'

            if (current_close > current_open and filter_env == 'bullish') or \
               (current_close < current_open and filter_env == 'bearish'):
                
                self.logger.warning(f"🚀 侦测到激增信号！将在接下来 {settings.SPIKE_GRACE_PERIOD_SECONDS} 秒内激活“超级激进”模式。")
                self.aggression_level = 2
                self.aggressive_mode_until = time.time() + settings.SPIKE_GRACE_PERIOD_SECONDS
                self.last_spike_analysis["status"] = "Super Aggressive Mode Activated"
                send_bark_notification(f"将在 {settings.SPIKE_GRACE_PERIOD_SECONDS}s 内寻找最激进的回调机会。", f"🚀 {self.symbol} 激增信号")

        except Exception as e:
            self.logger.error(f"检查激增信号时出错: {e}", exc_info=True)
            self.last_spike_analysis["status"] = "Error"

    async def get_entry_ema(self, ohlcv_data: list = None):
        """计算并返回用于入场判断的EMA值 (可接收外部数据)"""
        try:
            # 如果外部没有提供数据，则自己获取
            if ohlcv_data is None:
                self.logger.debug("get_entry_ema 正在独立获取K线数据...")
                ohlcv_data = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_SIGNAL_TIMEFRAME, limit=futures_settings.FUTURES_ENTRY_PULLBACK_EMA_PERIOD + 5)
            
            if not ohlcv_data or len(ohlcv_data) < futures_settings.FUTURES_ENTRY_PULLBACK_EMA_PERIOD:
                return None
            
            closes = np.array([c[4] for c in ohlcv_data])
            ema = pd.Series(closes).ewm(span=futures_settings.FUTURES_ENTRY_PULLBACK_EMA_PERIOD, adjust=False).mean().iloc[-1]
            return ema
        except Exception as e:
            self.logger.error(f"计算EMA失败: {e}")
            return None

    
    async def _log_status_snapshot(self, current_price: float, current_trend: str):
        try:
            balance_info = await self.exchange.fetch_balance({'type': 'swap'})
            total_equity = float(balance_info['total']['USDT'])
            pos = self.position.get_status()
            log_lines = ["----------------- 策略状态快照 -----------------"]
            
            if pos['is_open']:
                pnl = (current_price - pos['entry_price']) * pos['size'] if pos['side'] == 'long' else (pos['entry_price'] - current_price) * pos['size']
                margin = (pos['entry_price'] * pos['size'] / futures_settings.FUTURES_LEVERAGE)
                pnl_percent = (pnl / margin) * 100 if margin > 0 else 0
                dist_to_sl = abs((current_price - pos['stop_loss']) / pos['stop_loss']) * 100 if pos['stop_loss'] > 0 else float('inf')
                
                # --- [核心修改开始] ---

                # 初始化加仓目标行为空
                pyramiding_line = ""
                
                # 检查加仓功能是否启用，且尚未达到最大加仓次数
                if futures_settings.PYRAMIDING_ENABLED and pos['add_count'] < futures_settings.PYRAMIDING_MAX_ADD_COUNT:
                    initial_risk_per_unit = pos.get('initial_risk_per_unit', 0.0)
                    if initial_risk_per_unit > 0:
                        # 获取最初的开仓价
                        initial_entry_price = pos['entries'][0]['price']
                        
                        # 计算下一次加仓的目标乘数
                        next_target_multiplier = self.dyn_pyramiding_trigger * (pos['add_count'] + 1)
                        # 计算下一次加仓需要达到的盈利目标 (单位价格)
                        profit_target = initial_risk_per_unit * next_target_multiplier
                        
                        target_price = 0.0
                        if pos['side'] == 'long':
                            target_price = initial_entry_price + profit_target
                        else: # short
                            target_price = initial_entry_price - profit_target
                        
                        # 构建要显示的文本行
                        pyramiding_line = f"\n  - 下次加仓触发价: {target_price:.4f} ({next_target_multiplier:.2f}R)"

                # --- [核心修改结束] ---

                # 止盈目标行的逻辑保持不变
                take_profit_line = ""
                if pos.get('take_profit', 0.0) > 0:
                    dist_to_tp = abs((pos['take_profit'] - current_price) / current_price) * 100 if current_price > 0 else float('inf')
                    take_profit_line = f"\n  - 止盈目标: {pos['take_profit']:.4f} (距离 {dist_to_tp:.2f}%)"
                
                # 将加仓目标行和止盈目标行一起添加到最终的输出中
                log_lines.extend([
                    f"持仓状态: {pos['side'].upper()}ING", 
                    f"  - 开仓均价: {pos['entry_price']:.4f}", 
                    f"  - 持仓数量: {pos['size']:.5f}", 
                    f"  - 浮动盈亏: {pnl:+.2f} USDT ({pnl_percent:+.2f}%)",
                    f"  - 追踪止损: {pos['stop_loss']:.4f} (距离 {dist_to_sl:.2f}%)" + take_profit_line + pyramiding_line
                ])

            else:
                log_lines.append("持仓状态: 空仓等待信号")
                try:
                    ema = await self.get_entry_ema()
                    if ema is not None:
                        log_lines.append(f"  - 入场监控: 当前价({current_price:.4f}) vs EMA({ema:.4f})")
                    else:
                        log_lines.append("  - 入场监控: EMA数据获取中...")
                except: 
                    log_lines.append("  - 入场监控: EMA数据获取中...")
            
            log_lines.extend([f"市场判断: {current_trend.upper()}", f"账户权益: {total_equity:.2f} USDT", "----------------------------------------------------"])
            self.logger.info("\n" + "\n".join(log_lines))
        except Exception as e:
            self.logger.warning(f"打印状态快照时出错: {e}")

    async def get_atr_data(self, period=14, ohlcv_data: list = None):
        """计算并返回ATR(平均真实波幅)值 (可接收外部数据)"""
        try:
            # 如果外部没有提供数据，则自己获取
            if ohlcv_data is None:
                self.logger.debug("get_atr_data 正在独立获取K线数据...")
                ohlcv_data = await self.exchange.fetch_ohlcv(self.symbol, timeframe='15m', limit=period + 100)
            
            if not ohlcv_data or len(ohlcv_data) < period:
                self.logger.warning("ATR计算所需K线数据不足")
                return None
            
            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            
            tr = np.max(pd.concat([high_low, high_close, low_close], axis=1), axis=1)
            atr = tr.ewm(alpha=1/period, adjust=False).mean()
            return atr.iloc[-1]
        except Exception as e:
            self.logger.error(f"计算ATR失败: {e}"); return None
   


    async def _update_trailing_stop(self, current_price: float):
        """
        [V4.1 - 修复日志] 两阶段动态止损系统。
        - 阶段一: 使用基于当前价格的紧密ATR追踪，快速实现保本。
        - 阶段二: 当利润达到阈值后，切换为基于波段极值的宽松吊灯止损，以捕捉大趋势。
        """
        if not self.position.is_position_open():
            return

        # 如果在config中禁用了此功能，则直接返回
        if not futures_settings.CHANDELIER_EXIT_ENABLED:
            if self.position.is_position_open(): # 仅在有仓位时提示一次
                 self.logger.debug("两阶段动态止损系统已禁用。")
            return

        pos = self.position.get_status()
        initial_risk_per_unit = pos.get('initial_risk_per_unit', 0.0)
        if initial_risk_per_unit <= 0:
            self.logger.debug("初始风险(1R)为0，跳过追踪止损。")
            return

        # --- 计算当前浮动盈利 ---
        initial_entry_price = pos['entries'][0]['price']
        pnl_per_unit = (current_price - initial_entry_price) if pos['side'] == 'long' else (initial_entry_price - current_price)
        profit_multiple = pnl_per_unit / initial_risk_per_unit if initial_risk_per_unit > 0 else 0

        # --- 检查是否满足从阶段1切换到阶段2的条件 ---
        if pos['sl_stage'] == 1:
            if profit_multiple >= futures_settings.CHANDELIER_ACTIVATION_PROFIT_MULTIPLE:
                self.position.advance_sl_stage(2)
                pos['sl_stage'] = 2 
                send_bark_notification(
                    f"浮动盈利已达 {profit_multiple:.2f}R，超过 {futures_settings.CHANDELIER_ACTIVATION_PROFIT_MULTIPLE}R 门槛。",
                    f"💡 {self.symbol} 止损策略升级为吊灯模式"
                )

        # --- 根据当前阶段执行不同的止损逻辑 ---
        new_stop_loss = 0.0
        reason = ""
        log_details = "" # [新增日志] 用于存储计算细节

        # --- 阶段一：常规ATR追踪止损 ---
        if pos['sl_stage'] == 1:
            activation_threshold = initial_risk_per_unit * 1.0
            if pnl_per_unit < activation_threshold:
                # [新增日志] 明确告知用户为何不移动止损
                self.logger.info(f"止损阶段 {pos['sl_stage']}: 浮盈 {pnl_per_unit:.4f} 未达到激活门槛 {activation_threshold:.4f}，暂不移动止损。")
                return

            atr = await self.get_atr_data(period=14)
            if atr is None: return

            if pos['side'] == 'long':
                new_stop_loss = current_price - (atr * self.dyn_atr_multiplier)
            else:
                new_stop_loss = current_price + (atr * self.dyn_atr_multiplier)
            
            reason = "ATR Trailing"
            log_details = f"市价={current_price:.4f}, ATR={atr:.4f}, 乘数={self.dyn_atr_multiplier:.2f}"

        # --- 阶段二：吊灯止损 (Chandelier Exit) ---
        elif pos['sl_stage'] == 2:
            try:
                atr = await self.get_atr_data(period=14)
                ohlcv_data = await self.exchange.fetch_ohlcv(
                    self.symbol, 
                    timeframe='15m', 
                    limit=futures_settings.CHANDELIER_PERIOD + 5
                )
                if atr is None or not ohlcv_data or len(ohlcv_data) < futures_settings.CHANDELIER_PERIOD:
                    self.logger.warning("吊灯止损计算数据不足，跳过本次更新。")
                    return
                
                df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                if pos['side'] == 'long':
                    highest_high = df['high'].rolling(window=futures_settings.CHANDELIER_PERIOD).max().iloc[-1]
                    new_stop_loss = highest_high - (atr * futures_settings.CHANDELIER_ATR_MULTIPLIER)
                    log_details = f"{futures_settings.CHANDELIER_PERIOD}周期最高价={highest_high:.4f}, ATR={atr:.4f}"
                else: # short
                    lowest_low = df['low'].rolling(window=futures_settings.CHANDELIER_PERIOD).min().iloc[-1]
                    new_stop_loss = lowest_low + (atr * futures_settings.CHANDELIER_ATR_MULTIPLIER)
                    log_details = f"{futures_settings.CHANDELIER_PERIOD}周期最低价={lowest_low:.4f}, ATR={atr:.4f}"

                reason = "Chandelier Exit"

            except Exception as e:
                self.logger.error(f"计算吊灯止损时出错: {e}", exc_info=True)
                return

        # [新增日志] 统一打印计算过程，无论是否移动
        self.logger.info(
            f"止损计算 ({reason}): "
            f"当前SL={pos['stop_loss']:.4f}, 计算SL={new_stop_loss:.4f} | "
            f"细节: {log_details}"
        )

        # --- 最后，调用更新方法 ---
        if new_stop_loss > 0 and reason:
            self.position.update_stop_loss(new_stop_loss, reason=reason)


    async def get_bollinger_bands_data(self, ohlcv_data: list = None):
        """计算并返回最新的布林带上、中、下轨值 (可接收外部数据)"""
        try:
            # 如果外部没有提供数据，则自己获取
            if ohlcv_data is None:
                self.logger.debug("get_bollinger_bands_data 正在独立获取K线数据...")
                ohlcv_data = await self.exchange.fetch_ohlcv(
                    self.symbol, 
                    timeframe=settings.BREAKOUT_TIMEFRAME, 
                    limit=settings.BREAKOUT_BBANDS_PERIOD + 5
                )

            if not ohlcv_data or len(ohlcv_data) < settings.BREAKOUT_BBANDS_PERIOD:
                return None

            closes = pd.Series([c[4] for c in ohlcv_data])
            
            middle_band = closes.rolling(window=settings.BREAKOUT_BBANDS_PERIOD).mean()
            std_dev = closes.rolling(window=settings.BREAKOUT_BBANDS_PERIOD).std()
            upper_band = middle_band + (std_dev * settings.BREAKOUT_BBANDS_STD_DEV)
            lower_band = middle_band - (std_dev * settings.BREAKOUT_BBANDS_STD_DEV)

            return {
                "upper": upper_band.iloc[-2],
                "middle": middle_band.iloc[-2],
                "lower": lower_band.iloc[-2]
            }
        except Exception as e:
            self.logger.error(f"计算布林带数据时出错: {e}", exc_info=True)
            return None


    async def _check_breakout_signal(self):
        """[修改] 作为信号发射器，激活“激进”模式，并使用独立的宏观方向过滤器"""
        if not settings.ENABLE_BREAKOUT_MODIFIER or self.position.is_position_open():
            return

        # 如果当前已经处于任何激进模式中，则不进行干预
        if time.time() < self.aggressive_mode_until:
            return

        try:
            # 1. 获取布林带数据和价格
            bbands = await self.get_bollinger_bands_data()
            if bbands is None: return
            
            ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.BREAKOUT_TIMEFRAME, limit=2)
            if not ohlcv or len(ohlcv) < 2: return
            last_closed_price = ohlcv[-2][4] # 使用-2来明确表示是倒数第二根，即最后一根完整K线

            # 2. 获取15m宏观环境作为方向过滤器
            filter_ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_FILTER_TIMEFRAME, limit=settings.TREND_FILTER_MA_PERIOD + 2)
            if not filter_ohlcv or len(filter_ohlcv) < settings.TREND_FILTER_MA_PERIOD: return
            
            filter_closes = np.array([c[4] for c in filter_ohlcv])
            filter_ma = np.mean(filter_closes[-settings.TREND_FILTER_MA_PERIOD:])
            filter_env = 'bullish' if last_closed_price > filter_ma else 'bearish'

            # 3. 判断突破
            breakout_detected = False
            if filter_env == 'bullish' and last_closed_price > bbands['upper']:
                breakout_detected = True
            elif filter_env == 'bearish' and last_closed_price < bbands['lower']:
                breakout_detected = True
            
            if breakout_detected:
                self.logger.warning(f"🎯 侦测到突破信号！将在接下来 {settings.BREAKOUT_GRACE_PERIOD_SECONDS} 秒内激活“激进”模式。")
                self.aggression_level = 1
                self.aggressive_mode_until = time.time() + settings.BREAKOUT_GRACE_PERIOD_SECONDS
                send_bark_notification(
                    f"价格: {last_closed_price:.4f}，将在 {settings.BREAKOUT_GRACE_PERIOD_SECONDS}s 内放宽审查标准。",
                    f"🎯 {self.symbol} 突破信号"
                )
        except Exception as e:
            self.logger.error(f"检查突破信号时出错: {e}", exc_info=True)

    async def _check_entry_signal(self, current_trend: str, current_price: float):
        """[修改] 回调入场，集成对不同激进等级的回调区放宽"""
        if self.position.is_position_open() or current_trend == 'sideways':
            return None
        
        try:
            ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_SIGNAL_TIMEFRAME, limit=futures_settings.FUTURES_ENTRY_PULLBACK_EMA_PERIOD + 5)
            if not ohlcv: return None
            closes = np.array([c[4] for c in ohlcv])
            ema = pd.Series(closes).ewm(span=futures_settings.FUTURES_ENTRY_PULLBACK_EMA_PERIOD, adjust=False).mean().iloc[-1]

            is_aggressive_mode_active = time.time() < self.aggressive_mode_until
            
            pullback_zone_percent = self.dyn_pullback_zone_percent
            mode_name = "常规"
            if is_aggressive_mode_active:
                if self.aggression_level == 2 and settings.ENABLE_SPIKE_MODIFIER:
                    pullback_zone_percent *= settings.SUPER_AGGRESSIVE_PULLBACK_ZONE_MULTIPLIER
                    mode_name = "超级激进"
                elif self.aggression_level == 1 and settings.ENABLE_BREAKOUT_MODIFIER:
                    pullback_zone_percent *= settings.AGGRESSIVE_PULLBACK_ZONE_MULTIPLIER
                    mode_name = "激进"
            
            zone_multiplier = pullback_zone_percent / 100.0
            upper_bound = ema * (1 + zone_multiplier)
            lower_bound = ema * (1 - zone_multiplier)

            self.logger.info(
                f"[调试] 回调检查 (模式: {mode_name}): 价格={current_price:.4f} | "
                f"机会区=[{lower_bound:.4f} - {upper_bound:.4f}]"
            )
            
            entry_side = None
            if current_trend == 'uptrend' and lower_bound <= current_price <= upper_bound:
                self.logger.info(f"📈 做多入场信号: 价格({current_price:.4f})已进入回调机会区。")
                entry_side = 'long'
            elif current_trend == 'downtrend' and lower_bound <= current_price <= upper_bound:
                self.logger.info(f"📉 做空入场信号: 价格({current_price:.4f})已进入反弹机会区。")
                entry_side = 'short'
            
            if entry_side:
                self.aggressive_mode_until = 0
                self.aggression_level = 0
                return entry_side
                
            return None
            
        except Exception as e:
            self.logger.error(f"检查入场信号时出错: {e}", exc_info=True)
            return None


    async def _check_exit_signal(self, current_price: float):
        """
        【V3 离场优化版】
        1. (最高优先级) 检查是否触及动态更新的追踪止损位。
        2. (可选) 保留固定止盈作为备用。
        3. (已移除) 不再使用敏感的趋势判断作为主要平仓依据，避免被短期波动震荡出局。
        """
        if not self.position.is_position_open():
            return None # 确保计数器在空仓时被忽略
        
        try:
            pos = self.position.get_status()
            
            # 1. 检查动态追踪止损 (最高优先级)
            if (pos['side'] == 'long' and current_price <= pos['stop_loss']) or \
               (pos['side'] == 'short' and current_price >= pos['stop_loss']):
                self.logger.warning(f"🚨 追踪止损离场: {pos['side']}仓位价格({current_price:.4f})触及动态止损线({pos['stop_loss']:.4f})。")
                return 'trailing_stop_loss'

            # 2. 【核心修改】通过检查止盈价是否大于0，来决定是否执行这块逻辑。
            # 因为我们在开仓时已经将其设为0，所以这块代码永远不会被执行，从而安全地禁用了该功能。
            if pos.get('take_profit', 0.0) > 0:
                if (pos['side'] == 'long' and current_price >= pos['take_profit']) or \
                   (pos['side'] == 'short' and current_price <= pos['take_profit']):
                    self.logger.info(f"✅ 固定止盈离场: {pos['side']}仓位价格({current_price:.4f})触及止盈线({pos['take_profit']:.4f})。")
                    return 'take_profit'
            
            return None

        except Exception as e:
            self.logger.error(f"检查出场信号时出错: {e}", exc_info=True)
            return None

    async def confirm_order_filled(self, order_id, timeout=60, interval=2):
        """循环查询订单状态，直到确认成交或超时"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                order = await self.exchange.fetch_order(order_id, self.symbol)
                if order['status'] == 'closed':
                    self.logger.info(f"订单 {order_id} 已确认成交 (均价: {order['average']})。")
                    return order
                await asyncio.sleep(interval)
            except NetworkError as e:
                self.logger.warning(f"确认订单网络错误，重试: {e}"); await asyncio.sleep(interval * 2)
            except Exception as e:
                self.logger.error(f"确认订单时未知错误: {e}", exc_info=True); return None
        return None    


    async def execute_trade(self, action: str, side: str = None, reason: str = ''):
        """【修改】增加 reason 参数，用于记录开仓原因"""
        try:
            if action == 'open' and side:
                market = self.exchange.exchange.market(self.symbol)
                entry_price = (await self.exchange.fetch_ticker(self.symbol))['last']
                sl_percent = futures_settings.FUTURES_STOP_LOSS_PERCENT / 100
                balance_info = await self.exchange.fetch_balance({'type': 'swap'})
                total_equity = float(balance_info['total']['USDT'])
                risk_amount_per_trade = total_equity * (futures_settings.FUTURES_RISK_PER_TRADE_PERCENT / 100)
                price_diff_per_unit = entry_price * sl_percent
                if price_diff_per_unit <= 0: self.logger.error("止损距离为0，取消开仓。"); return

                position_size_by_risk = risk_amount_per_trade / price_diff_per_unit
                min_notional = market.get('limits', {}).get('cost', {}).get('min', 20.0)
                min_position_size = min_notional / entry_price if entry_price > 0 else float('inf')
                final_position_size = max(position_size_by_risk, min_position_size)

                amount_precision_float = market.get('precision', {}).get('amount')
                if amount_precision_float is None: raise ValueError(f"无法获取 {self.symbol} 的数量精度")
                import math
                position_size_rounded_up = math.ceil(final_position_size / amount_precision_float) * amount_precision_float
                position_size_formatted = self.exchange.exchange.amount_to_precision(self.symbol, position_size_rounded_up)

                api_side = 'buy' if side == 'long' else 'sell'
                self.logger.info(f"准备开仓: {side.upper()} | 目标名义价值 > {min_notional} USDT | 最终格式化数量: {position_size_formatted}")
                order = await self.exchange.create_market_order(self.symbol, api_side, position_size_formatted)
                
                filled_order = await self.confirm_order_filled(order['id'])
                if not filled_order: self.logger.critical(f"开仓订单 {order['id']} 超时未确认！请手动检查！"); return
                
                filled_price = filled_order['average']
                filled_size = filled_order['filled']
                order_timestamp = filled_order['timestamp']
                fee_info = filled_order.get('fee')
                entry_fee = fee_info.get('cost', 0.0) if fee_info else 0.0
                
                stop_loss_price = filled_price * (1 - sl_percent) if side == 'long' else filled_price * (1 + sl_percent)
                take_profit_price = 0.0

                # --- [核心修改] 将入场原因传递给 PositionTracker ---
                # 如果 reason 为空 (例如手动触发)，则给一个默认值
                entry_reason = reason if reason else 'unknown'
                self.position.open_position(side, filled_price, filled_size, entry_fee, stop_loss_price, take_profit_price, order_timestamp, entry_reason)
                
                title = f"📈 开仓 {side.upper()} {self.symbol} (原因: {entry_reason})"
                content = f"价格: {filled_price:.4f}\n数量: {filled_size:.5f}\n手续费: {entry_fee:.4f} USDT\n初始止损: {stop_loss_price:.4f}"
                send_bark_notification(content, title)

            elif action == 'close':
                if not self.position.is_position_open(): return
                closed_position = self.position.get_status()
                close_side, size = ('sell' if self.position.side == 'long' else 'buy'), self.position.size
                params = {'reduceOnly': True}
                self.logger.info(f"准备平仓: {self.position.side.upper()} | 数量: {size:.8f} | 原因: {reason}")
                formatted_size = self.exchange.exchange.amount_to_precision(self.symbol, size)
                order = await self.exchange.create_market_order(self.symbol, close_side, formatted_size, params)
                filled_order = await self.confirm_order_filled(order['id'])
                if not filled_order: self.logger.critical(f"平仓订单 {order['id']} 超时未确认！请手动检查！"); return
                
                closing_fee_info = filled_order.get('fee')
                closing_fee = closing_fee_info.get('cost', 0.0) if closing_fee_info else 0.0
                opening_fee = closed_position.get('entry_fee', 0.0)

                gross_pnl = (filled_order['average'] - closed_position['entry_price']) * closed_position['size'] if closed_position['side'] == 'long' else (closed_position['entry_price'] - filled_order['average']) * closed_position['size']
                net_pnl = gross_pnl - opening_fee - closing_fee
                
                self.profit_tracker.add_profit(net_pnl)
                self.position.close_position()
                
                pnl_str = f"+{net_pnl:.2f}" if net_pnl >= 0 else f"{net_pnl:.2f}"
                title = f"💰 平仓 {closed_position['side'].upper()} {self.symbol} | 净利润: {pnl_str} USDT"
                content = f"平仓原因: {reason}\n开仓价: {closed_position['entry_price']:.4f}\n平仓价: {filled_order['average']:.4f}\n总手续费: {(opening_fee + closing_fee):.4f}"
                send_bark_notification(content, title)

        except (InsufficientFunds, ExchangeError, Exception) as e:
            error_type = type(e).__name__
            self.logger.error(f"执行交易({action}, {side})时发生 {error_type} 错误: {e}", exc_info=True)
            send_bark_notification(f"交易执行失败: {e}", f"‼️ {self.symbol} 交易错误")


    async def _handle_trend_disagreement(self, current_trend: str, current_price: float):
        """增加激增入境宽限期检查，并传递原因"""
        if not futures_settings.TREND_EXIT_ADJUST_SL_ENABLED or not self.position.is_position_open():
            return

        pos = self.position.get_status()

        if pos.get('entry_reason') == 'spike_entry' and pos.get('entries'):
            entry_timestamp = pos['entries'][0].get('timestamp', 0)
            grace_period_ms = settings.SPIKE_ENTRY_GRACE_PERIOD_MINUTES * 60 * 1000
            if (time.time() * 1000 - entry_timestamp) < grace_period_ms:
                self.logger.info(f"激增信号入场宽限期内，跳过趋势不一致检查。")
                self.trend_exit_counter = 0
                return
        
        trend_is_adverse = (pos['side'] == 'long' and current_trend != 'uptrend') or \
                           (pos['side'] == 'short' and current_trend != 'downtrend')

        if trend_is_adverse:
            self.trend_exit_counter += 1
            self.logger.info(f"持仓方向({pos['side'].upper()})与趋势({current_trend.upper()})不符，确认计数: {self.trend_exit_counter}/{futures_settings.TREND_EXIT_CONFIRMATION_COUNT}")

            if self.trend_exit_counter >= futures_settings.TREND_EXIT_CONFIRMATION_COUNT:
                self.logger.warning(f"趋势已连续 {self.trend_exit_counter} 次与持仓方向不符，触发防御性止损！")
                atr = await self.get_atr_data(period=14)
                if atr is None:
                    self.logger.warning("无法获取ATR数据，本次无法调整止损。")
                    return

                new_stop_loss = 0.0
                if pos['side'] == 'long':
                    new_stop_loss = current_price - (atr * futures_settings.TREND_EXIT_ATR_MULTIPLIER)
                else:
                    new_stop_loss = current_price + (atr * futures_settings.TREND_EXIT_ATR_MULTIPLIER)
                
                self.position.update_stop_loss(new_stop_loss, reason="Defensive Adjustment")
                
                self.trend_exit_counter = 0
        else:
            if self.trend_exit_counter > 0:
                self.logger.info("趋势已恢复与持仓方向一致，重置确认计数器。")
                self.trend_exit_counter = 0

    async def _check_and_execute_pyramiding(self, current_price: float, current_trend: str):
        """[最终版] 加仓后，智能选择“保本点”与“ATR追踪”中更优的止损位"""
        if not futures_settings.PYRAMIDING_ENABLED or not self.position.is_position_open():
            return

        pos_status = self.position.get_status()
        
        if pos_status['add_count'] >= futures_settings.PYRAMIDING_MAX_ADD_COUNT:
            return

        if (pos_status['side'] == 'long' and current_trend != 'uptrend') or \
           (pos_status['side'] == 'short' and current_trend != 'downtrend'):
            self.logger.info(f"加仓检查：趋势({current_trend})已不符，取消加仓。")
            return

        initial_risk_per_unit = pos_status.get('initial_risk_per_unit', 0.0)
        if initial_risk_per_unit == 0: 
            self.logger.warning("初始风险(1R)为0，无法计算加仓目标。")
            return

        initial_entry_price = pos_status['entries'][0]['price']
        if pos_status['side'] == 'long':
            unrealized_pnl_per_unit = current_price - initial_entry_price
        else:
            unrealized_pnl_per_unit = initial_entry_price - current_price
        
        next_target_multiplier = self.dyn_pyramiding_trigger * (pos_status['add_count'] + 1)
        profit_target = initial_risk_per_unit * next_target_multiplier
        
        if unrealized_pnl_per_unit < profit_target:
            return
            
        self.logger.info(f"✅ 满足第 {pos_status['add_count'] + 1} 次加仓条件！浮动盈利已达到目标 {next_target_multiplier:.2f}R。")
        
        last_entry = self.position.entries[-1]
        last_size = last_entry['size']
        add_size = last_size * futures_settings.PYRAMIDING_ADD_SIZE_RATIO
        
        formatted_add_size = self.exchange.exchange.amount_to_precision(self.symbol, add_size)
        api_side = 'buy' if pos_status['side'] == 'long' else 'sell'
        
        try:
            self.logger.info(f"准备加仓: {pos_status['side'].upper()} | 数量: {formatted_add_size}")
            order = await self.exchange.create_market_order(self.symbol, api_side, formatted_add_size)
            filled_order = await self.confirm_order_filled(order['id'])
            
            if not filled_order:
                self.logger.error("加仓订单未能确认成交，本次加仓失败。")
                return

            filled_price = filled_order['average']
            filled_size = filled_order['filled']
            order_timestamp = filled_order['timestamp']
            fee_info = filled_order.get('fee')
            entry_fee = fee_info.get('cost', 0.0) if fee_info else 0.0
            
            self.position.add_to_position(filled_price, filled_size, entry_fee, order_timestamp)

            new_pos_status = self.position.get_status()
            title = f"➕ {self.symbol} 浮盈加仓成功 ({new_pos_status['add_count']}/{futures_settings.PYRAMIDING_MAX_ADD_COUNT})"
            content = (f"方向: {new_pos_status['side'].upper()}\n"
                       f"加仓价格: {filled_price:.4f}\n"
                       f"加仓数量: {filled_size:.5f}\n"
                       f"--- 更新后 ---\n"
                       f"平均成本: {new_pos_status['entry_price']:.4f}\n"
                       f"总仓位: {new_pos_status['size']:.5f}")
            send_bark_notification(content, title)

            break_even_price = self.position.break_even_price

            atr = await self.get_atr_data(period=14)
            atr_stop_loss = 0.0
            if atr is not None:
                if new_pos_status['side'] == 'long':
                    atr_stop_loss = current_price - (atr * self.dyn_atr_multiplier)
                else:
                    atr_stop_loss = current_price + (atr * self.dyn_atr_multiplier)

            if new_pos_status['side'] == 'long':
                final_stop_loss = max(break_even_price, atr_stop_loss)
            else:
                final_stop_loss = min(break_even_price, atr_stop_loss)
            
            self.logger.info(f"加仓后，比较保本点({break_even_price:.4f})与ATR止损({atr_stop_loss:.4f})，选择更优的({final_stop_loss:.4f})作为新止损。")
            self.position.update_stop_loss(final_stop_loss, reason="Pyramiding Secure")

        except Exception as e:
            self.logger.error(f"执行加仓时发生错误: {e}", exc_info=True)

    async def main_loop(self):
        """策略主循环 (已优化信号检查顺序)"""
        if not self.initialized: await self.initialize()
        while True:
            try:
                current_price = (await self.exchange.fetch_ticker(self.symbol))['last']
                if not current_price:
                    self.logger.warning("无法获取当前价格，本次循环跳过。"); await asyncio.sleep(5); continue
                
                current_time = time.time()

                if current_time - self.last_perf_check_time >= settings.PERFORMANCE_CHECK_INTERVAL_HOURS * 3600:
                    await self._update_dynamic_parameters()
                    self.last_perf_check_time = current_time

                if not self.position.is_position_open():
                    entry_side = None
                    entry_reason = None

                    # --- [核心重构] 调整信号检查顺序 ---
                    # 1. (最高优先级) 检查“激增”信号，它会设置 aggression_level = 2
                    await self._check_spike_entry_signal()
                    
                    # 2. 检查“突破”信号，它会设置 aggression_level = 1. 它现在是独立的
                    await self._check_breakout_signal()
                    
                    # 3. 运行慢速、可靠的趋势判断, 它会读取 aggression_level 来放宽审查
                    current_trend = await self._detect_trend()
                    
                    if current_time - self.last_status_log_time >= 60:
                        await self._log_status_snapshot(current_price, current_trend)
                        self.last_status_log_time = current_time
                    
                    # 4. 最后检查“回调”信号，它会读取 aggression_level 来放宽回调区
                    entry_side = await self._check_entry_signal(current_trend, current_price)
                    if entry_side:
                        if time.time() < self.aggressive_mode_until:
                            if self.aggression_level == 2:
                                entry_reason = 'spike_pullback'
                            elif self.aggression_level == 1:
                                entry_reason = 'breakout_pullback'
                        else:
                            entry_reason = 'pullback_entry'
                    
                    if entry_side: 
                        await self.execute_trade('open', side=entry_side, reason=entry_reason)
                else:
                    # 持仓逻辑 (保持不变)
                    current_trend = await self._detect_trend()
                    if current_time - self.last_status_log_time >= 60:
                        await self._log_status_snapshot(current_price, current_trend)
                        self.last_status_log_time = current_time
                    
                    await self._check_and_execute_pyramiding(current_price, current_trend)
                    await self._handle_trend_disagreement(current_trend, current_price)
                    await self._update_trailing_stop(current_price)
                    exit_reason = await self._check_exit_signal(current_price)
                    if exit_reason: await self.execute_trade('close', reason=exit_reason)
                
                await asyncio.sleep(10)
            except Exception as e:
                self.logger.critical(f"主循环发生致命错误，将等待60秒后重试: {e}", exc_info=True)
                await asyncio.sleep(60)
