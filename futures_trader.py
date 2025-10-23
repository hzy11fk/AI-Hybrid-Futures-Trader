import logging
import asyncio
import time
import numpy as np
import pandas as pd
import ccxt
from ccxt.base.errors import ExchangeError, NetworkError, InsufficientFunds
from config import futures_settings, settings
from position_tracker import PositionTracker
from helpers import send_bark_notification, extract_fee
from profit_tracker import ProfitTracker
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
        self.last_breakout_analysis = {}
        self.last_breakout_timestamp = 0
        self.profit_tracker = ProfitTracker(
            state_dir=futures_settings.FUTURES_STATE_DIR,
            symbol=self.symbol,
            initial_principal=settings.FUTURES_INITIAL_PRINCIPAL
        )
        self.last_trendline_analysis = {}
        self.trend_exit_counter = 0
        self.trend_confirmed_state = 'sideways'
        self.trend_grace_period_counter = 0
        self.trend_confirmation_timestamp = 0

        self.aggressive_mode_until = 0
        self.aggression_level = 0
        self.last_spike_timestamp = 0
        self.last_funding_check_time = 0
        self.last_perf_check_time = 0
        self.notifications_enabled = True
        # --- [核心修改] 新增用于UI展示的状态字典 ---
        self.last_momentum_analysis = {}
        self.last_exhaustion_analysis = {}
        self.last_trailing_stop_update_time = 0

        self.taker_fee_rate = 0.0005
        self.min_trade_amount = 0.001

        self.dyn_pullback_zone_percent = (settings.AGGRESSIVE_PARAMS['PULLBACK_ZONE_PERCENT'] + settings.DEFENSIVE_PARAMS['PULLBACK_ZONE_PERCENT']) / 2
        self.dyn_atr_multiplier = (settings.AGGRESSIVE_PARAMS['ATR_MULTIPLIER'] + settings.DEFENSIVE_PARAMS['ATR_MULTIPLIER']) / 2
        self.dyn_pyramiding_trigger = (settings.AGGRESSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE'] + settings.DEFENSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE']) / 2

    async def _sync_funding_fees(self):
        if not settings.ENABLE_FUNDING_FEE_SYNC: return
        current_time = time.time()
        if current_time - self.last_funding_check_time < settings.FUNDING_FEE_SYNC_INTERVAL_HOURS * 3600: return
        self.logger.info("开始同步资金费用流水...")
        try:
            last_ts = self.profit_tracker.last_funding_fee_timestamp
            since = last_ts + 1 if last_ts > 0 else None
            market = self.exchange.exchange.market(self.symbol)
            params = {'symbol': market['id'], 'incomeType': 'FUNDING_FEE'}
            if since: params['startTime'] = since
            income_history = await self.exchange.exchange.fapiPrivateGetIncome(params)
            if income_history: self.profit_tracker.add_funding_fees(income_history)
            else: self.logger.info("未发现新的资金费用记录。")
            self.last_funding_check_time = current_time
        except Exception as e:
            self.logger.error(f"同步资金费用时发生错误: {e}", exc_info=True)

    async def _find_and_analyze_trendlines(self, ohlcv_data: list, current_price: float):
        self.last_trendline_analysis = { "support_price": None, "resistance_price": None }
        lookback = settings.TRENDLINE_LOOKBACK_PERIOD
        window = settings.TRENDLINE_PIVOT_WINDOW
        if len(ohlcv_data) < lookback:
            return None, None
        df = pd.DataFrame(ohlcv_data[-lookback:], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['is_swing_low'] = (df['low'] == df['low'].rolling(window=2*window+1, center=True, min_periods=window+1).min())
        df['is_swing_high'] = (df['high'] == df['high'].rolling(window=2*window+1, center=True, min_periods=window+1).max())
        swing_lows = df[df['is_swing_low']].copy()
        swing_highs = df[df['is_swing_high']].copy()
        support_line, resistance_line = None, None
        if len(swing_lows) >= 2:
            p1, p2 = swing_lows.iloc[-2], swing_lows.iloc[-1]
            slope = (p2['low'] - p1['low']) / (p2['timestamp'] - p1['timestamp']) if (p2['timestamp'] - p1['timestamp']) != 0 else 0
            support_line = {'p1_ts': p1['timestamp'], 'p1_price': p1['low'], 'slope': slope}
        if len(swing_highs) >= 2:
            p1, p2 = swing_highs.iloc[-2], swing_highs.iloc[-1]
            slope = (p2['high'] - p1['high']) / (p2['timestamp'] - p1['timestamp']) if (p2['timestamp'] - p1['timestamp']) != 0 else 0
            resistance_line = {'p1_ts': p1['timestamp'], 'p1_price': p1['high'], 'slope': slope}
        current_ts = ohlcv_data[-1][0]
        if support_line:
            self.last_trendline_analysis['support_price'] = support_line['p1_price'] + (current_ts - support_line['p1_ts']) * support_line['slope']
        if resistance_line:
            self.last_trendline_analysis['resistance_price'] = resistance_line['p1_price'] + (current_ts - resistance_line['p1_ts']) * resistance_line['slope']
        return support_line, resistance_line

    async def initialize(self):
        try:
            await self.exchange.load_markets()
            market_info = self.exchange.exchange.market(self.symbol)
            self.min_trade_amount = market_info.get('limits', {}).get('amount', {}).get('min', 0.001)
            if self.min_trade_amount is None or self.min_trade_amount == 0.0: self.min_trade_amount = 0.001
            self.taker_fee_rate = market_info.get('taker', self.taker_fee_rate)
            self.logger.info(f"已加载市场信息, Taker费率: {self.taker_fee_rate * 100:.4f}%, 最小交易量: {self.min_trade_amount}")
            if self.profit_tracker.is_new: await self._initialize_profit_from_history()
            self.logger.info(f"正在为 {self.symbol} 设置杠杆为 {futures_settings.FUTURES_LEVERAGE}x...")
            await self.exchange.set_leverage(futures_settings.FUTURES_LEVERAGE, self.symbol)
            self.logger.info(f"正在为 {self.symbol} 设置保证金模式为 {futures_settings.FUTURES_MARGIN_MODE}...")
            await self.exchange.set_margin_mode(futures_settings.FUTURES_MARGIN_MODE, self.symbol)
            self.logger.info(f"合约趋势策略初始化成功: {self.symbol}")
            self.initialized = True
        except ExchangeError as e:
            self.logger.warning(f"设置杠杆或保证金模式可能失败: {e}"); self.initialized = True
        except Exception as e:
            self.logger.error(f"初始化失败: {e}", exc_info=True); self.initialized = False


    async def get_bollinger_bands_data(self, ohlcv_data: list = None, period: int = None, std_dev: float = None, check_squeeze: bool = False):
        try:
            bb_period = period if period is not None else settings.BREAKOUT_BBANDS_PERIOD
            bb_std_dev = std_dev if std_dev is not None else settings.BREAKOUT_BBANDS_STD_DEV
            
            # --- [核心修改] 根据是否需要检查挤压状态，动态确定所需数据长度 ---
            if check_squeeze and settings.ENABLE_BBAND_SQUEEZE_FILTER:
                required_limit = bb_period + settings.BBAND_SQUEEZE_LOOKBACK_PERIOD + 5
            else:
                required_limit = bb_period + 2 # 只需要足够计算BBands即可
            
            if ohlcv_data is None: 
                # 注意：如果外部不提供数据，这里的timeframe可能需要根据场景调整，但目前够用
                ohlcv_data = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.BREAKOUT_TIMEFRAME, limit=required_limit)
            
            if not ohlcv_data or len(ohlcv_data) < required_limit: 
                self.logger.warning(f"BBands计算失败：数据长度 {len(ohlcv_data)} < 要求长度 {required_limit}")
                return None
            
            closes = pd.Series([c[4] for c in ohlcv_data])
            middle_band = closes.rolling(window=bb_period).mean()
            rolling_std = closes.rolling(window=bb_period).std()
            upper_band = middle_band + (rolling_std * bb_std_dev)
            lower_band = middle_band - (rolling_std * bb_std_dev)

            is_squeeze = False
            bandwidth_value = None
            
            # --- [核心修改] 只有在明确要求时，才计算挤压状态 ---
            if check_squeeze and settings.ENABLE_BBAND_SQUEEZE_FILTER:
                bandwidth = (upper_band - lower_band) / middle_band.replace(0, 1e-9)
                bandwidth_value = bandwidth.iloc[-2]
                
                if len(bandwidth.dropna()) > settings.BBAND_SQUEEZE_LOOKBACK_PERIOD:
                    squeeze_threshold = bandwidth.iloc[-(settings.BBAND_SQUEEZE_LOOKBACK_PERIOD + 2) : -2].quantile(settings.BBAND_SQUEEZE_THRESHOLD_PERCENTILE)
                    if not np.isnan(bandwidth_value) and not np.isnan(squeeze_threshold) and bandwidth_value < squeeze_threshold:
                        is_squeeze = True

            if len(upper_band) >= 2 and not np.isnan(upper_band.iloc[-2]):
                 return {
                     "upper": upper_band.iloc[-2], 
                     "middle": middle_band.iloc[-2], 
                     "lower": lower_band.iloc[-2],
                     "bandwidth": bandwidth_value,
                     "is_squeeze": is_squeeze
                 }
            return None
        except Exception as e:
            self.logger.error(f"计算布林带数据时出错: {e}", exc_info=True); return None


    async def _initialize_profit_from_history(self):
        self.logger.warning(f"[{self.symbol}] 利润账本文件不存在，尝试从交易所历史成交初始化...")
        try:
            trades = await self.exchange.fetch_my_trades(self.symbol, limit=1000)
            if not trades:
                self.logger.info(f"[{self.symbol}] 未在交易所找到历史成交记录。")
                return

            trades.sort(key=lambda x: x.get('timestamp', 0))

            from collections import deque
            open_positions = deque()
            all_historical_trades = []

            for trade in trades:
                trade_side = trade.get('side')
                trade_amount = trade.get('amount')
                trade_price = trade.get('price')
                trade_timestamp = trade.get('timestamp')
                trade_fee = extract_fee(trade)
                
                if not all([trade_side, trade_amount > 0, trade_price > 0, trade_timestamp > 0]):
                    continue

                amount_to_match = trade_amount

                while amount_to_match > 1e-9 and open_positions and open_positions[0]['side'] != trade_side:
                    open_trade = open_positions[0]
                    matched_amount = min(amount_to_match, open_trade['amount'])
                    
                    pos_side = open_trade['side']
                    entry_price = open_trade['price']
                    exit_price = trade_price
                    entry_timestamp = open_trade['timestamp']
                    exit_timestamp = trade_timestamp

                    proportional_entry_fee = (open_trade.get('fee', 0.0) / open_trade['amount']) * matched_amount if open_trade.get('amount', 0) > 0 else 0
                    proportional_exit_fee = (trade_fee / trade_amount) * matched_amount if trade_amount > 0 else 0
                    total_fee = proportional_entry_fee + proportional_exit_fee
                    
                    if pos_side == 'long':
                        net_pnl = (exit_price - entry_price) * matched_amount - total_fee
                    else: # short
                        net_pnl = (entry_price - exit_price) * matched_amount - total_fee
                    
                    trade_record = {
                        "symbol": self.symbol, "side": pos_side, "entry_price": entry_price, 
                        "exit_price": exit_price, "size": matched_amount, "entry_timestamp": entry_timestamp, 
                        "exit_timestamp": exit_timestamp, "net_pnl": net_pnl, "reason": "historical_import"
                    }
                    all_historical_trades.append(trade_record)

                    amount_to_match -= matched_amount
                    open_trade['amount'] -= matched_amount

                    if open_trade['amount'] < 1e-9:
                        open_positions.popleft()

                if amount_to_match > 1e-9:
                    fee_for_open = (trade_fee / trade_amount) * amount_to_match if trade_amount > 0 else 0
                    open_positions.append({
                        'side': trade_side, 'amount': amount_to_match, 'price': trade_price, 
                        'timestamp': trade_timestamp, 'fee': fee_for_open
                    })

            if all_historical_trades:
                all_historical_trades.sort(key=lambda x: x.get('exit_timestamp', 0))
                self.logger.info(f"[{self.symbol}] 历史成交分析完成，成功重建 {len(all_historical_trades)} 笔已平仓交易。")
                for record in all_historical_trades:
                    self.profit_tracker.record_trade(record)
                self.logger.info(f"[{self.symbol}] 历史交易已成功导入利润账本。")
            else:
                self.logger.info(f"[{self.symbol}] 在历史记录中未能匹配任何完整的买卖交易对。")

        except Exception as e:
            self.logger.error(f"[{self.symbol}] 从历史成交初始化利润账本时发生未知错误: {e}", exc_info=True)

    async def _update_dynamic_parameters(self):
        if not settings.ENABLE_PERFORMANCE_FEEDBACK: return
        score = self.profit_tracker.get_performance_score()
        if score is None: self.logger.info("交易历史不足，暂不进行动态参数调整。"); return
        self.logger.info(f"策略综合表现得分: {score:.3f}，开始调整动态参数...")
        def interpolate(agg, d, s): return d + (agg - d) * s
        self.dyn_pullback_zone_percent = interpolate(settings.AGGRESSIVE_PARAMS['PULLBACK_ZONE_PERCENT'], settings.DEFENSIVE_PARAMS['PULLBACK_ZONE_PERCENT'], score)
        self.dyn_atr_multiplier = interpolate(settings.AGGRESSIVE_PARAMS['ATR_MULTIPLIER'], settings.DEFENSIVE_PARAMS['ATR_MULTIPLIER'], score)
        self.dyn_pyramiding_trigger = interpolate(settings.AGGRESSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE'], settings.DEFENSIVE_PARAMS['PYRAMIDING_TRIGGER_PROFIT_MULTIPLE'], score)
        log_msg = (f"动态参数已更新 (得分: {score:.3f}):\n"
                   f"  - 回调区参数: {self.dyn_pullback_zone_percent:.2f}%\n"
                   f"  - ATR止损参数: {self.dyn_atr_multiplier:.2f}\n"
                   f"  - 加仓触发倍数: {self.dyn_pyramiding_trigger:.2f}")
        self.logger.warning(log_msg)
        if self.notifications_enabled:
            send_bark_notification(log_msg, f"⚙️ {self.symbol} 策略参数自适应调整")


    async def get_adx_data(self, period=14, ohlcv_df: pd.DataFrame = None, return_series: bool = False):
        """
        [V2 - 升级版] 计算ADX指标。
        - 增加 return_series 参数，可以选择返回单个最终值或整个ADX序列。
        - 统一并修正了计算逻辑。
        """
        try:
            if ohlcv_df is None:
                ohlcv = await self.exchange.fetch_ohlcv(self.symbol, timeframe='15m', limit=period * 10)
                if not ohlcv: return None
                ohlcv_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            if len(ohlcv_df) < period + 1: return None
            
            high = ohlcv_df['high']
            low = ohlcv_df['low']
            close = ohlcv_df['close']
            
            # 标准的TR, +DM, -DM 计算
            move_up = high.diff()
            move_down = low.diff().mul(-1)
            
            plus_dm = pd.Series(np.where((move_up > move_down) & (move_up > 0), move_up, 0), index=ohlcv_df.index)
            minus_dm = pd.Series(np.where((move_down > move_up) & (move_down > 0), move_down, 0), index=ohlcv_df.index)

            tr1 = pd.DataFrame(high - low)
            tr2 = pd.DataFrame(abs(high - close.shift(1)))
            tr3 = pd.DataFrame(abs(low - close.shift(1)))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            # 使用 Wilder's Smoothing (等同于 alpha = 1/period 的 EWM)
            atr = tr.ewm(alpha=1/period, adjust=False).mean()
            plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-9))
            minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-9))
            
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1e-9)
            adx = dx.ewm(alpha=1/period, adjust=False).mean()
            
            if adx.empty: return None

            # 根据参数返回序列或单个值
            return adx if return_series else adx.iloc[-1]

        except Exception as e:
            self.logger.error(f"计算ADX失败: {e}", exc_info=True)
            return None


    async def _detect_trend(self, ohlcv_5m: list = None, ohlcv_15m: list = None):
        try:
            self.last_trend_analysis = { "filter_env": "N/A", "signal_trend": "N/A", "final_trend": "sideways", "confirmation": "N/A", "details": {} }
            if ohlcv_5m is None or ohlcv_15m is None: ohlcv_5m, ohlcv_15m = await asyncio.gather(self.exchange.fetch_ohlcv(self.symbol, settings.TREND_SIGNAL_TIMEFRAME, 150), self.exchange.fetch_ohlcv(self.symbol, settings.TREND_FILTER_TIMEFRAME, 150))
            if not all([ohlcv_5m, ohlcv_15m]): return 'sideways'
            ohlcv_15m_df = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            adx_value = await self.get_adx_data(period=14, ohlcv_df=ohlcv_15m_df)
            self.last_trend_analysis["details"]["adx"] = f"{adx_value:.2f}" if adx_value is not None else "N/A"
            filter_ma_series = ohlcv_15m_df['close'].ewm(span=settings.TREND_FILTER_MA_PERIOD, adjust=False).mean()
            if len(filter_ma_series) < 10: return 'sideways'
            filter_ma_slope = filter_ma_series.iloc[-1] - filter_ma_series.iloc[-10]
            filter_env = 'bullish' if filter_ma_slope > 0 else 'bearish' if filter_ma_slope < 0 else 'neutral'
            self.last_trend_analysis["filter_env"] = filter_env
            signal_df = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            current_price = signal_df['close'].iloc[-1]
            short_ma, long_ma = signal_df['close'].rolling(window=settings.TREND_SHORT_MA_PERIOD).mean().iloc[-1], signal_df['close'].rolling(window=settings.TREND_LONG_MA_PERIOD).mean().iloc[-1]
            if np.isnan(short_ma) or np.isnan(long_ma) or long_ma == 0: return 'sideways'
            diff_ratio = (short_ma - long_ma) / long_ma
            tr = np.max(pd.concat([signal_df['high'] - signal_df['low'], np.abs(signal_df['high'] - signal_df['close'].shift()), np.abs(signal_df['low'] - signal_df['close'].shift())], axis=1), axis=1)
            atr_value = tr.ewm(span=14, adjust=False).mean().iloc[-1]
            ATR_MULTIPLIER = 1.0
            if adx_value is not None:
                if adx_value > settings.TREND_ADX_THRESHOLD_STRONG: ATR_MULTIPLIER = settings.TREND_ATR_MULTIPLIER_STRONG
                elif adx_value < settings.TREND_ADX_THRESHOLD_WEAK: ATR_MULTIPLIER = settings.TREND_ATR_MULTIPLIER_WEAK
            dynamic_threshold = (atr_value / current_price) * ATR_MULTIPLIER if current_price > 0 else 0
            signal_trend = 'sideways'
            if diff_ratio > dynamic_threshold: signal_trend = 'uptrend'
            elif diff_ratio < -dynamic_threshold: signal_trend = 'downtrend'
            self.last_trend_analysis["signal_trend"] = signal_trend
            ma_based_trend = 'sideways'
            if signal_trend == 'uptrend' and (filter_env == 'bullish' or filter_env == 'neutral'): ma_based_trend = 'uptrend'
            elif signal_trend == 'downtrend' and (filter_env == 'bearish' or filter_env == 'neutral'): ma_based_trend = 'downtrend'
            price_trend_result = ma_based_trend
            ranging_enabled = getattr(settings, 'ENABLE_RANGING_STRATEGY', False)
            ranging_adx_threshold = getattr(settings, 'RANGING_ADX_THRESHOLD', 20)
            if ranging_enabled and ma_based_trend == 'sideways':
                if adx_value is not None and adx_value < ranging_adx_threshold:
                    price_trend_result = 'sideways'
                    self.logger.info(f"市场状态确认为 震荡: 均线不符且ADX({adx_value:.2f}) < 阈值({ranging_adx_threshold})。")
                else:
                    price_trend_result = 'uncertain'
                    self.logger.info(f"市场状态不明确，保持观望: 均线不符，但ADX({adx_value:.2f}) >= 震荡阈值({ranging_adx_threshold})。")
            if settings.ENABLE_TREND_MEMORY:
                current_kline_timestamp = ohlcv_5m[-1][0]
                if price_trend_result == self.trend_confirmed_state:
                    self.trend_grace_period_counter = settings.TREND_CONFIRMATION_GRACE_PERIOD
                    self.trend_confirmation_timestamp = current_kline_timestamp
                else:
                    if self.trend_grace_period_counter > 0 and current_kline_timestamp > self.trend_confirmation_timestamp:
                        price_trend_result = self.trend_confirmed_state
                        self.trend_grace_period_counter -= 1
                        self.trend_confirmation_timestamp = current_kline_timestamp
                    else:
                        self.trend_confirmed_state = 'sideways'
                        self.trend_grace_period_counter = 0
            if not self.position.is_position_open():
                if price_trend_result in ['sideways', 'uncertain']:
                    self.last_trend_analysis["final_trend"] = price_trend_result
                    return price_trend_result
                if settings.ENABLE_TREND_MEMORY and self.trend_confirmed_state != price_trend_result:
                    self.trend_confirmed_state = price_trend_result
                    self.trend_grace_period_counter = settings.TREND_CONFIRMATION_GRACE_PERIOD
                    self.trend_confirmation_timestamp = ohlcv_5m[-1][0]
                self.last_trend_analysis["final_trend"] = price_trend_result
                return price_trend_result
            self.last_trend_analysis["final_trend"] = price_trend_result
            return price_trend_result
        except Exception as e:
            self.logger.error(f"趋势过滤器 _detect_trend 发生严重错误: {e}", exc_info=True)
            return 'sideways'

    async def _check_spike_entry_signal(self, ohlcv_5m: list = None, ohlcv_15m: list = None):
        if not settings.ENABLE_SPIKE_MODIFIER or self.position.is_position_open(): return
        try:
            self.last_spike_analysis = {"status": "Monitoring...","current_body": None, "body_threshold": None,"current_volume": None, "volume_threshold": None}
            if ohlcv_5m is None: ohlcv_5m = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.SPIKE_TIMEFRAME, limit=50)
            if not ohlcv_5m or len(ohlcv_5m) < max(settings.TREND_VOLUME_CONFIRM_PERIOD, 14) + 2: self.last_spike_analysis["status"] = "OHLCV data insufficient"; return
            last_closed_candle = ohlcv_5m[-2]
            candle_timestamp, candle_open, _, _, candle_close, candle_volume = last_closed_candle
            atr = await self.get_atr_data(period=14, ohlcv_data=ohlcv_15m)
            current_body = abs(candle_close - candle_open)
            body_threshold = atr * settings.SPIKE_BODY_ATR_MULTIPLIER if atr else 0
            vma = np.mean([c[5] for c in ohlcv_5m[:-1]][-settings.TREND_VOLUME_CONFIRM_PERIOD:])
            volume_threshold = vma * settings.SPIKE_VOLUME_MULTIPLIER
            self.last_spike_analysis.update({"current_body": current_body, "body_threshold": body_threshold,"current_volume": candle_volume, "volume_threshold": volume_threshold})
            if atr is None or current_body < body_threshold: self.last_spike_analysis["status"] = "Body too small"; return
            if candle_volume < volume_threshold: self.last_spike_analysis["status"] = "Volume too low"; return
            signal_direction = 'long' if candle_close > candle_open else 'short'
            if settings.REQUIRE_FILTER_FOR_AGGRESSIVE:
                if ohlcv_15m is None: ohlcv_15m = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_FILTER_TIMEFRAME, limit=settings.TREND_FILTER_MA_PERIOD + 2)
                if not ohlcv_15m or len(ohlcv_15m) < settings.TREND_FILTER_MA_PERIOD: self.last_spike_analysis["status"] = "Filter data insufficient"; return
                filter_ma = np.mean([c[4] for c in ohlcv_15m][-settings.TREND_FILTER_MA_PERIOD:])
                filter_env = 'bullish' if candle_close > filter_ma else 'bearish'
                if (signal_direction == 'long' and filter_env != 'bullish') or (signal_direction == 'short' and filter_env != 'bearish'):
                    self.logger.info(f"激增信号 ({signal_direction}) 因与15m宏观趋势 ({filter_env}) 不符而被过滤。"); self.last_spike_analysis["status"] = f"Filtered by macro trend ({filter_env})"; return
            self.last_spike_analysis["status"] = f"Triggered! ({signal_direction})"
            self.last_spike_timestamp = candle_timestamp
            self.logger.warning(f"🚀 侦测到激增信号！将在 {settings.SPIKE_ENTRY_CONFIRMATION_BARS} 根K线后寻找机会。")
            self.aggression_level, self.aggressive_mode_until = 2, time.time() + settings.SPIKE_GRACE_PERIOD_SECONDS
        except Exception as e:
            self.logger.error(f"检查激增信号时出错: {e}", exc_info=True); self.last_spike_analysis["status"] = "Error"

    async def get_entry_ema(self, ohlcv_data: list = None, period: int = None):
        try:
            target_period = period or futures_settings.FUTURES_ENTRY_PULLBACK_EMA_PERIOD
            if ohlcv_data is None: ohlcv_data = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_SIGNAL_TIMEFRAME, limit=target_period + 5)
            if not ohlcv_data or len(ohlcv_data) < target_period: return None
            return pd.Series([c[4] for c in ohlcv_data]).ewm(span=target_period, adjust=False).mean().iloc[-1]
        except Exception as e:
            self.logger.error(f"计算EMA失败: {e}"); return None

    async def _log_status_snapshot(self, current_price: float, current_trend: str, filter_ma_value: [float, str] = "N/A", ohlcv_15m: list = None):
        try:
            balance_info = await self.exchange.fetch_balance({'type': 'swap'})
            total_equity = float(balance_info.get('total', {}).get('USDT', 0.0))
            pos = self.position.get_status()
            log_lines = ["----------------- 策略状态快照 -----------------"]
            
            if pos.get('is_open'):
                entry_reason = pos.get('entry_reason')
                if entry_reason == 'breakout_momentum_trade': log_lines.append("交易模式: ⚡️ 突破动能 (持仓中)")
                elif entry_reason == 'ranging_entry': log_lines.append("交易模式: ⚖️ 均值回归 (持仓中)")
                else: log_lines.append("交易模式: 📈 趋势跟踪 (持仓中)")
            else:
                ranging_enabled = getattr(settings, 'ENABLE_RANGING_STRATEGY', False)
                if ranging_enabled and current_trend == 'sideways': log_lines.append("交易模式: ⚖️ 均值回归 (等待信号)")
                else: log_lines.append("交易模式: 📈 趋势跟踪 (等待信号)")

            if isinstance(filter_ma_value, float): log_lines.append(f"宏观MA ({settings.TREND_FILTER_TIMEFRAME} | {settings.TREND_FILTER_MA_PERIOD}): {filter_ma_value:.4f}")
            else: log_lines.append(f"宏观MA ({settings.TREND_FILTER_TIMEFRAME} | {settings.TREND_FILTER_MA_PERIOD}): {filter_ma_value}")
            log_lines.append(f"当前价格: {current_price:.4f}")
            
            if pos.get('is_open'):
                pnl = (current_price - pos['entry_price']) * pos['size'] if pos['side'] == 'long' else (pos['entry_price'] - current_price) * pos['size']
                margin = (pos['entry_price'] * pos['size'] / futures_settings.FUTURES_LEVERAGE)
                pnl_percent = (pnl / margin) * 100 if margin > 0 else 0
                dist_to_sl = abs((current_price - pos['stop_loss']) / pos['stop_loss']) * 100 if pos.get('stop_loss', 0.0) > 0 else float('inf')
                
                pyramiding_line, take_profit_line, ranging_tp_line = "", "", ""

                if pos.get('entry_reason') == 'ranging_entry' and ohlcv_15m:
                    bbands = await self.get_bollinger_bands_data(
                        ohlcv_data=ohlcv_15m,
                        period=settings.RANGING_BBANDS_PERIOD,
                        std_dev=settings.RANGING_BBANDS_STD_DEV
                    )
                    if bbands and bbands.get('middle') and settings.RANGING_TAKE_PROFIT_TARGET == 'middle':
                        tp_price = bbands['middle']
                        dist_to_tp = abs((tp_price - current_price) / current_price) * 100 if current_price > 0 else float('inf')
                        ranging_tp_line = f"\n  - 均值回归止盈: {tp_price:.4f} (中轨, 距离 {dist_to_tp:.2f}%)"

                if futures_settings.PYRAMIDING_ENABLED and pos.get('add_count', 0) < futures_settings.PYRAMIDING_MAX_ADD_COUNT and pos.get('initial_risk_per_unit', 0) > 0 and pos.get('entries'):
                    next_target_multiplier = self.dyn_pyramiding_trigger * (pos['add_count'] + 1)
                    profit_target = pos['initial_risk_per_unit'] * next_target_multiplier
                    target_price = pos['entries'][0]['price'] + profit_target if pos['side'] == 'long' else pos['entries'][0]['price'] - profit_target
                    pyramiding_line = f"\n  - 下次加仓触发价: {target_price:.4f} ({next_target_multiplier:.2f}R)"
                
                if pos.get('take_profit', 0.0) > 0:
                    dist_to_tp = abs((pos['take_profit'] - current_price) / current_price) * 100 if current_price > 0 else float('inf')
                    take_profit_line = f"\n  - 止盈目标: {pos['take_profit']:.4f} (距离 {dist_to_tp:.2f}%)"
                
                log_lines.extend([
                    f"持仓状态: {pos.get('side', 'N/A').upper()}ING",
                    f"  - 开仓均价: {pos.get('entry_price', 0.0):.4f}",
                    f"  - 持仓数量: {pos.get('size', 0.0):.5f}",
                    f"  - 浮动盈亏: {pnl:+.2f} USDT ({pnl_percent:+.2f}%)",
                    f"  - 追踪止损: {pos.get('stop_loss', 0.0):.4f} (距离 {dist_to_sl:.2f}%)" + take_profit_line + pyramiding_line + ranging_tp_line
                ])
            else: 
                log_lines.append("持仓状态: 空仓等待信号")
            
            log_lines.append(f"市场判断: {current_trend.upper()}")
            log_lines.append(f"账户权益: {total_equity:.2f} USDT")
            log_lines.append("----------------------------------------------------")
            self.logger.info("\n" + "\n".join(log_lines))
        except Exception as e:
            self.logger.warning(f"打印状态快照时出错: {e}", exc_info=True)


    async def get_rsi_data(self, period: int, ohlcv_data: list = None):
        try:
            if ohlcv_data is None: ohlcv_data = await self.exchange.fetch_ohlcv(self.symbol, timeframe=settings.TREND_SIGNAL_TIMEFRAME, limit=period + 50)
            if not ohlcv_data or len(ohlcv_data) < period + 1: return None
            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))
            return rsi.iloc[-1]
        except Exception as e:
            self.logger.error(f"计算RSI失败: {e}", exc_info=True); return None

    async def get_atr_data(self, period=14, ohlcv_data: list = None):
        try:
            if ohlcv_data is None: ohlcv_data = await self.exchange.fetch_ohlcv(self.symbol, timeframe='15m', limit=period + 100)
            if not ohlcv_data or len(ohlcv_data) < 2: return None
            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            tr = np.max(pd.concat([df['high'] - df['low'], np.abs(df['high'] - df['close'].shift()), np.abs(df['low'] - df['close'].shift())], axis=1), axis=1)
            return tr.ewm(span=period, adjust=False).mean().iloc[-1]
        except Exception as e:
            self.logger.error(f"计算ATR失败: {e}"); return None

    async def _update_trailing_stop(self, current_price: float, current_trend: str, ohlcv_5m: list, ohlcv_15m: list) -> bool:
        if not self.position.is_position_open(): return False
        now = time.time()
        if now - self.last_trailing_stop_update_time < futures_settings.TRAILING_STOP_MIN_UPDATE_SECONDS: return False
        pos = self.position.get_status()
        old_stop_loss = pos['stop_loss']
        atr_15m_long = await self.get_atr_data(period=max(futures_settings.CHANDELIER_PERIOD, futures_settings.TRAILING_STOP_ATR_LONG_PERIOD), ohlcv_data=ohlcv_15m)
        atr_5m_short = await self.get_atr_data(period=futures_settings.TRAILING_STOP_ATR_SHORT_PERIOD, ohlcv_data=ohlcv_5m)
        if atr_15m_long is None or atr_15m_long == 0: return False
        if atr_15m_long < current_price * futures_settings.TRAILING_STOP_VOLATILITY_PAUSE_THRESHOLD: return False
        final_atr_multiplier, vol_ratio = self.dyn_atr_multiplier, 1.0
        if futures_settings.ADAPTIVE_TRAILING_STOP_ENABLED and atr_5m_short is not None and atr_15m_long > 0:
            vol_ratio = atr_5m_short / atr_15m_long
            final_atr_multiplier = self.dyn_atr_multiplier * (1 + max(0, vol_ratio - 1) * 0.5)
            final_atr_multiplier = min(final_atr_multiplier, self.dyn_atr_multiplier * 2)
        initial_risk_per_unit = pos.get('initial_risk_per_unit', 0.0)
        if initial_risk_per_unit <= 0: return False
        pnl_per_unit = (current_price - pos['entries'][0]['price']) if pos['side'] == 'long' else (pos['entries'][0]['price'] - current_price)
        profit_multiple = pnl_per_unit / initial_risk_per_unit if initial_risk_per_unit > 0 else 0
        if pos['sl_stage'] == 1 and profit_multiple >= futures_settings.CHANDELIER_ACTIVATION_PROFIT_MULTIPLE:
            self.position.advance_sl_stage(2); pos['sl_stage'] = 2
        candidate_stop_loss, reason = 0.0, ""
        if pos['sl_stage'] == 1:
            if profit_multiple < 1.0: return False
            candidate_stop_loss = current_price - (atr_15m_long * final_atr_multiplier) if pos['side'] == 'long' else current_price + (atr_15m_long * final_atr_multiplier)
            reason = "ATR Trailing"
        elif pos['sl_stage'] == 2:
            df = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            if pos['side'] == 'long':
                highest_high = df['high'].rolling(window=futures_settings.CHANDELIER_PERIOD).max().iloc[-1]
                candidate_stop_loss = highest_high - (atr_15m_long * futures_settings.CHANDELIER_ATR_MULTIPLIER)
            else:
                lowest_low = df['low'].rolling(window=futures_settings.CHANDELIER_PERIOD).min().iloc[-1]
                candidate_stop_loss = lowest_low + (atr_15m_long * futures_settings.CHANDELIER_ATR_MULTIPLIER)
            reason = "Chandelier Exit"
        updated = self.position.update_stop_loss(candidate_stop_loss, reason=reason)
        if updated: self.last_trailing_stop_update_time = now
        return updated


    async def _check_breakout_signal(self, ohlcv_5m: list = None, ohlcv_15m: list = None):
        if not settings.ENABLE_BREAKOUT_MODIFIER or self.position.is_position_open(): return None
        # --- [核心修改] 更新UI状态字典 ---
        self.last_breakout_analysis = { "status": "Monitoring...", "squeeze_status": "N/A" }
        try:
            required_bars = max(settings.BREAKOUT_BBANDS_PERIOD, settings.BREAKOUT_VOLUME_PERIOD, settings.BREAKOUT_RSI_PERIOD) + 3
            if ohlcv_5m is None or len(ohlcv_5m) < required_bars: 
                self.last_breakout_analysis["status"] = "OHLCV data insufficient"; return None
            bbands = await self.get_bollinger_bands_data(ohlcv_data=ohlcv_5m, check_squeeze=True)
            if bbands is None: 
                self.last_breakout_analysis["status"] = "BBands calculation failed"; return None

            # --- [核心修改] 应用布林带挤压过滤器 ---
            if settings.ENABLE_BBAND_SQUEEZE_FILTER:
                self.last_breakout_analysis["squeeze_status"] = "Squeezed" if bbands['is_squeeze'] else "Not Squeezed"
                if not bbands['is_squeeze']:
                    self.last_breakout_analysis["status"] = "波动率过滤"
                    return None # 如果没有处于挤压状态，则直接返回，不判断后续突破
            # --- 修改结束 ---

            last_candle, prev_candle = ohlcv_5m[-2], ohlcv_5m[-3]
            is_long_breakout = (last_candle[4] > bbands['upper'] and prev_candle[4] <= bbands['upper'])
            is_short_breakout = (last_candle[4] < bbands['lower'] and prev_candle[4] >= bbands['lower'])
            
            if not is_long_breakout and not is_short_breakout: return None
            
            signal_direction = 'long' if is_long_breakout else 'short'
            self.last_breakout_analysis["status"] = f"穿越信号 ({signal_direction})"
            
            if settings.BREAKOUT_VOLUME_CONFIRMATION:
                df = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                volume_threshold = df['volume'].iloc[-(settings.BREAKOUT_VOLUME_PERIOD + 1):-1].mean() * settings.BREAKOUT_VOLUME_MULTIPLIER
                self.last_breakout_analysis.update({"volume": last_candle[5], "volume_threshold": volume_threshold})
                if last_candle[5] < volume_threshold: self.last_breakout_analysis["status"] = "成交量过滤"; return None
            
            if settings.BREAKOUT_RSI_CONFIRMATION:
                rsi_value = await self.get_rsi_data(period=settings.BREAKOUT_RSI_PERIOD, ohlcv_data=ohlcv_5m)
                self.last_breakout_analysis.update({"rsi_value": rsi_value, "rsi_threshold": settings.BREAKOUT_RSI_THRESHOLD})
                if rsi_value is None: self.last_breakout_analysis["status"] = "RSI计算失败"; return None
                if (signal_direction == 'long' and rsi_value <= settings.BREAKOUT_RSI_THRESHOLD) or (signal_direction == 'short' and rsi_value >= (100 - settings.BREAKOUT_RSI_THRESHOLD)): # 修正short判断
                    self.last_breakout_analysis["status"] = "RSI动量过滤"; return None

            if time.time() - self.last_breakout_timestamp < settings.BREAKOUT_GRACE_PERIOD_SECONDS: 
                self.last_breakout_analysis["status"] = "冷却中"; return None
            
            self.last_breakout_timestamp = time.time(); self.last_breakout_analysis["status"] = f"触发成功! ({signal_direction})"
            self.logger.warning(f"🎯 侦测到经过确认的有效突破信号 ({signal_direction})！(源于低波动挤压)")
            return ('breakout_momentum_entry', signal_direction)
        except Exception as e:
            self.logger.error(f"检查突破信号时出错: {e}", exc_info=True); self.last_breakout_analysis["status"] = "Error"; return None


    async def _manage_breakout_momentum_stop(self, current_price: float):
        pos = self.position.get_status()
        self.position.update_price_mark(current_price)
        pos = self.position.get_status()
        new_stop_loss = 0.0
        if pos['side'] == 'long': new_stop_loss = pos['high_water_mark'] * (1 - settings.BREAKOUT_TRAIL_STOP_PERCENT)
        elif pos['side'] == 'short': new_stop_loss = pos['low_water_mark'] * (1 + settings.BREAKOUT_TRAIL_STOP_PERCENT)
        if self.position.update_stop_loss(new_stop_loss, reason="Breakout Momentum Trail"):
            self.logger.info(f"⚡️ 突破动能追踪止损已更新至: {new_stop_loss:.4f} (基于极值: {pos.get('high_water_mark') or pos.get('low_water_mark'):.4f})")

    async def _analyze_pullback_quality(self, entry_side: str, df: pd.DataFrame) -> bool:
        if not settings.ENABLE_PULLBACK_QUALITY_FILTER: return True
        try:
            short_ma = df['close'].rolling(window=settings.TREND_SHORT_MA_PERIOD).mean()
            long_ma = df['close'].rolling(window=settings.TREND_LONG_MA_PERIOD).mean()
            if entry_side == 'long':
                cross_indices = np.where(np.diff(np.sign(short_ma - long_ma)) > 0)[0]
                if len(cross_indices) == 0: return True
                trend_start_index = cross_indices[-1]
                trend_df = df.iloc[trend_start_index:]
                if trend_df.empty: return True
                pullback_start_index = trend_df['high'].idxmax()
                impulse_wave = df.iloc[trend_start_index:pullback_start_index+1]
                pullback_wave = df.iloc[pullback_start_index+1:]
            else:
                cross_indices = np.where(np.diff(np.sign(short_ma - long_ma)) < 0)[0]
                if len(cross_indices) == 0: return True
                trend_start_index = cross_indices[-1]
                trend_df = df.iloc[trend_start_index:]
                if trend_df.empty: return True
                pullback_start_index = trend_df['low'].idxmin()
                impulse_wave = df.iloc[trend_start_index:pullback_start_index+1]
                pullback_wave = df.iloc[pullback_start_index+1:]
            if impulse_wave.empty or pullback_wave.empty: return True
            avg_impulse_volume = impulse_wave['volume'].mean()
            avg_pullback_volume = pullback_wave['volume'].mean()
            if avg_impulse_volume > 0 and avg_pullback_volume > (avg_impulse_volume * settings.PULLBACK_MAX_VOLUME_RATIO):
                self.logger.warning(f"回调信号被过滤：回调成交量({avg_pullback_volume:.2f})过大。")
                return False
            return True
        except Exception as e:
            self.logger.error(f"回调质量分析时出错: {e}", exc_info=True); return True

    async def _confirm_momentum_rebound(self, entry_side: str, ohlcv_data: list) -> bool:
        """[V2 - UI支持版] 使用RSI确认回调结束，动能是否恢复。"""
        if not settings.ENABLE_ENTRY_MOMENTUM_CONFIRMATION:
            return True

        self.last_momentum_analysis = {"status": "Not Active", "rsi_value": None, "is_rebounding": False}

        try:
            required_bars = settings.ENTRY_RSI_PERIOD + settings.ENTRY_RSI_CONFIRMATION_BARS + 5
            if len(ohlcv_data) < required_bars:
                self.last_momentum_analysis["status"] = "Data Insufficient"
                return False

            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).ewm(alpha=1/settings.ENTRY_RSI_PERIOD, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/settings.ENTRY_RSI_PERIOD, adjust=False).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi_series = 100 - (100 / (1 + rs))
            
            if rsi_series.isnull().all() or len(rsi_series) < settings.ENTRY_RSI_CONFIRMATION_BARS:
                self.last_momentum_analysis["status"] = "Data Insufficient"
                return False

            last_n_rsi = rsi_series.iloc[-settings.ENTRY_RSI_CONFIRMATION_BARS:]
            rsi_diff = last_n_rsi.diff().dropna()
            current_rsi = last_n_rsi.iloc[-1]
            self.last_momentum_analysis["rsi_value"] = f"{current_rsi:.2f}"

            if entry_side == 'long':
                is_rebounding = not rsi_diff.empty and all(rsi_diff > 0)
                self.last_momentum_analysis["is_rebounding"] = is_rebounding
                if is_rebounding:
                    self.last_momentum_analysis["status"] = "✅ Passed"
                    self.logger.info(f"✅ 多头动能确认: RSI({current_rsi:.2f}) 连续回升。")
                    return True
                else:
                    self.last_momentum_analysis["status"] = "❌ Filtered"
                    self.logger.info(f"动能过滤：价格虽在回调区，但RSI({current_rsi:.2f})未显示持续回升。")
                    return False
            
            if entry_side == 'short':
                is_rebounding = not rsi_diff.empty and all(rsi_diff < 0)
                self.last_momentum_analysis["is_rebounding"] = is_rebounding
                if is_rebounding:
                    self.last_momentum_analysis["status"] = "✅ Passed"
                    self.logger.info(f"✅ 空头动能确认: RSI({current_rsi:.2f}) 连续回落。")
                    return True
                else:
                    self.last_momentum_analysis["status"] = "❌ Filtered"
                    self.logger.info(f"动能过滤：价格虽在回调区，但RSI({current_rsi:.2f})未显示持续回落。")
                    return False
            
            return False
        except Exception as e:
            self.logger.error(f"检查动能反弹时出错: {e}", exc_info=True)
            self.last_momentum_analysis["status"] = "Error"
            return False

    async def _check_entry_signal(self, current_trend: str, current_price: float, ohlcv_5m: list, ohlcv_15m: list):
        if self.position.is_position_open() or current_trend not in ['uptrend', 'downtrend']: return None
        try:
            ema_fast = await self.get_entry_ema(ohlcv_data=ohlcv_5m, period=10)
            ema_slow = await self.get_entry_ema(ohlcv_data=ohlcv_5m, period=20)
            if ema_fast is None or ema_slow is None: return None
            
            upper_bound, lower_bound = (max(ema_fast, ema_slow), min(ema_fast, ema_slow))
            entry_side = None
            if current_trend == 'uptrend' and lower_bound <= current_price <= upper_bound:
                entry_side = 'long'
            elif current_trend == 'downtrend' and lower_bound <= current_price <= upper_bound:
                entry_side = 'short'

            if not entry_side:
                return None
            is_aggressive_mode = self.aggressive_mode_until > time.time()

            if is_aggressive_mode:
                self.logger.warning("处于激增信号后的攻击模式中，将跳过RSI动能确认，直接入场！")
                momentum_confirmed = True
            else:
                self.logger.info(f"位置信号 ({entry_side}) 已触发，开始进行动能确认...")
                momentum_confirmed = await self._confirm_momentum_rebound(entry_side, ohlcv_5m)

            if not momentum_confirmed:
                return None

            df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            is_quality_pullback = await self._analyze_pullback_quality(entry_side, df_5m)
            if not is_quality_pullback:
                return None

            if settings.ENABLE_TRENDLINE_FILTER:
                # ... (您现有的趋势线代码逻辑) ...
                pass

            self.logger.warning(f"{'📈' if entry_side == 'long' else '📉'} 高质量入场信号: 价格({current_price:.4f})在回调区且通过所有过滤器。")
            return entry_side
                
        except Exception as e:
            self.logger.error(f"检查入场信号时出错: {e}", exc_info=True); return None

    async def _check_exit_signal(self, current_price: float):
        if not self.position.is_position_open(): return None
        try:
            pos = self.position.get_status()
            if (pos['side'] == 'long' and current_price <= pos['stop_loss']) or (pos['side'] == 'short' and current_price >= pos['stop_loss']): return 'trailing_stop_loss'
            if pos.get('take_profit', 0.0) > 0 and ((pos['side'] == 'long' and current_price >= pos['take_profit']) or (pos['side'] == 'short' and current_price <= pos['take_profit'])): return 'take_profit'
            return None
        except Exception as e:
            self.logger.error(f"检查出场信号时出错: {e}", exc_info=True); return None

    async def confirm_order_filled(self, order_id, timeout=60, interval=2):
        start_time = time.time()
        filled_order_data = None
        while time.time() - start_time < timeout:
            if filled_order_data is None:
                try:
                    order = await self.exchange.fetch_order(order_id, self.symbol)
                    if isinstance(order, dict) and order.get('status') == 'closed':
                        filled_order_data = order
                except NetworkError as e:
                    self.logger.warning(f"确认订单网络错误，重试: {e}"); await asyncio.sleep(interval * 2)
                except Exception as e:
                    self.logger.error(f"确认订单 {order_id} 时发生未知错误: {e}", exc_info=True); await asyncio.sleep(interval)
            if filled_order_data is not None: break
            await asyncio.sleep(interval)
        if filled_order_data: return filled_order_data
        else: self.logger.error(f"订单 {order_id} 确认超时！"); return None

    
    async def execute_trade(self, action: str, side: str = None, reason: str = '', size: float = None):
        logger = self.logger
        try:
            if action == 'open' and side:
                entry_price = (await self.exchange.fetch_ticker(self.symbol))['last']
                if not isinstance(entry_price, (int, float)) or entry_price <= 0: logger.error(f"获取价格无效 ({entry_price})，取消开仓。"); return
                balance_info = await self.exchange.fetch_balance({'type': 'swap'})
                total_equity = float(balance_info.get('total', {}).get('USDT', 0.0))
                available_balance = float(balance_info.get('free', {}).get('USDT', 0.0)) or total_equity
                if available_balance <= 0: logger.critical(f"账户余额为0，无法开仓。"); return
                leverage = futures_settings.FUTURES_LEVERAGE
                min_notional = getattr(futures_settings, 'MIN_NOMINAL_VALUE_USDT', 21.0)
                price_diff_per_unit = 0.0

                if reason == 'ranging_entry':
                    ohlcv_ranging = await self.exchange.fetch_ohlcv(self.symbol, settings.RANGING_TIMEFRAME, 150)
                    atr = await self.get_atr_data(period=14, ohlcv_data=ohlcv_ranging)
                    if atr is None or atr <= 0: logger.error(f"无法为震荡策略获取ATR，取消开仓。"); return
                    price_diff_per_unit = atr * settings.RANGING_STOP_LOSS_ATR_MULTIPLIER
                elif futures_settings.USE_ATR_FOR_INITIAL_STOP:
                    atr = await self.get_atr_data(period=14)
                    if atr is None or atr <= 0: logger.error(f"无法获取有效ATR，取消开仓。"); return
                    price_diff_per_unit = atr * futures_settings.INITIAL_STOP_ATR_MULTIPLIER
                else:
                    price_diff_per_unit = entry_price * (getattr(futures_settings, 'FUTURES_STOP_LOSS_PERCENT', 2.5) / 100)
                
                price_diff_per_unit = max(price_diff_per_unit, entry_price * 0.005)
                if price_diff_per_unit <= 0: logger.error(f"止损距离计算错误({price_diff_per_unit})，取消开仓。"); return

                final_pos_size = 0.0
                if reason == 'breakout_momentum_trade':
                    nominal_value = settings.BREAKOUT_NOMINAL_VALUE_USDT
                    final_pos_size = nominal_value / entry_price
                    logger.info(f"应用 [突破] 策略仓位: 名义价值 ${nominal_value:.2f}")
                elif reason == 'ranging_entry':
                    nominal_value = settings.RANGING_NOMINAL_VALUE_USDT
                    final_pos_size = nominal_value / entry_price
                    logger.info(f"应用 [震荡] 策略仓位: 名义价值 ${nominal_value:.2f}")
                else:
                    risk_amount = total_equity * (futures_settings.FUTURES_RISK_PER_TRADE_PERCENT / 100)
                    pos_size_by_risk = risk_amount / price_diff_per_unit
                    logger.info(f"应用 [趋势] 策略仓位: 风险金额 ${risk_amount:.2f}, 风险计算数量 {pos_size_by_risk:.5f}")
                    if pos_size_by_risk * entry_price < min_notional:
                        final_pos_size = min_notional / entry_price
                        logger.warning(f"风险计算仓位过小，使用最小名义价值 ${min_notional:.2f} 开仓。")
                    else:
                        final_pos_size = pos_size_by_risk

                required_margin = (final_pos_size * entry_price) / leverage
                max_allowed_margin = total_equity * futures_settings.MAX_MARGIN_PER_TRADE_RATIO
                
                if required_margin > max_allowed_margin:
                    original_size = final_pos_size
                    final_pos_size = (max_allowed_margin * leverage) / entry_price
                    logger.warning(
                        f"!!! 仓位自动调整 !!!\n"
                        f"  - 计算所需保证金 ({required_margin:.2f} USDT) 超出单笔上限 ({max_allowed_margin:.2f} USDT)。\n"
                        f"  - 将自动缩减仓位以符合保证金上限进行开仓。\n"
                        f"  - 原始计算数量: {original_size:.8f}, 调整后数量: {final_pos_size:.8f}"
                    )
                
                if final_pos_size <= 0: logger.error(f"计算仓位为0或负数({final_pos_size})，取消开仓。"); return
                if (final_pos_size * entry_price / leverage) > available_balance: logger.critical(f"保证金不足！需要: {(final_pos_size * entry_price / leverage):.2f}, 可用: {available_balance:.2f}。"); return
                final_pos_size = max(final_pos_size, self.min_trade_amount)
                pos_size_fmt = self.exchange.exchange.amount_to_precision(self.symbol, final_pos_size)
                if float(pos_size_fmt) <= 0: logger.error(f"格式化后仓位为0({pos_size_fmt})，取消开仓。"); return
                
                api_side = 'buy' if side == 'long' else 'sell'
                order = await self.exchange.create_market_order(self.symbol, api_side, pos_size_fmt)
                filled_order = await self.confirm_order_filled(order['id'])
                if not isinstance(filled_order, dict): logger.critical(f"开仓订单 {order['id']} 确认失败。"); return
                filled_price, filled_size, ts = filled_order.get('average'), filled_order.get('filled'), filled_order.get('timestamp')
                if not all([isinstance(v, (int, float)) and v > 0 for v in [filled_price, filled_size, ts]]): logger.error(f"成交订单字段无效: {filled_order}。"); return
                entry_fee = extract_fee(filled_order)
                sl_price = filled_price - price_diff_per_unit if side == 'long' else filled_price + price_diff_per_unit
                self.position.open_position(side, filled_price, filled_size, entry_fee, sl_price, 0.0, ts, reason)

                if self.notifications_enabled:
                    send_bark_notification(f"价格: {filled_price:.4f}\n数量: {filled_size:.5f}\n止损: {sl_price:.4f}\n原因: {reason}", f"📈 开仓 {side.upper()} {self.symbol}")
            
            elif action == 'close':
                if not self.position.is_position_open(): return
                pos = self.position.get_status()
                close_side, size_to_close = ('sell' if pos['side'] == 'long' else 'buy'), pos['size']
                if size_to_close <= 0: return
                fmt_size = self.exchange.exchange.amount_to_precision(self.symbol, size_to_close)
                if float(fmt_size) <= 0: return
                order = await self.exchange.create_market_order(self.symbol, close_side, fmt_size, {'reduceOnly': True})
                filled_order = await self.confirm_order_filled(order['id'])
                if not isinstance(filled_order, dict): logger.critical(f"平仓订单 {order['id']} 超时未确认！请手动检查！"); return
                closing_fee = extract_fee(filled_order)
                exit_price, entry_price, pos_size = filled_order.get('average'), pos['entry_price'], pos['size']
                if not all([isinstance(v, (int, float)) for v in [exit_price, entry_price, pos_size]]): logger.error(f"计算平仓盈亏数据无效。"); return
                gross_pnl = (exit_price - entry_price) * pos_size if pos['side'] == 'long' else (entry_price - exit_price) * pos_size
                net_pnl = gross_pnl - pos['entry_fee'] - closing_fee
                trade_record = {"symbol": self.symbol, "side": pos['side'], "entry_price": entry_price, "exit_price": exit_price, "size": pos_size, "entry_timestamp": pos['entries'][0]['timestamp'] if pos.get('entries') else 0, "exit_timestamp": filled_order.get('timestamp', 0), "net_pnl": net_pnl, "reason": reason}
                if hasattr(self, 'profit_tracker'): self.profit_tracker.record_trade(trade_record)
                self.position.close_position()
                pnl_str = f"+{net_pnl:.2f}" if net_pnl >= 0 else f"{net_pnl:.2f}"

                if self.notifications_enabled:
                    send_bark_notification(f"原因: {reason}\n开仓均价: {entry_price:.4f}\n平仓价: {exit_price:.4f}", f"💰 平仓 {pos['side'].upper()} | 净利: {pnl_str} USDT")

            elif action == 'partial_close':
                if not self.position.is_position_open() or size is None or size <= 0: return
                pos = self.position.get_status()
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                size_to_close = min(size, pos['size'])
                if size_to_close <= 0: return
                fmt_size = self.exchange.exchange.amount_to_precision(self.symbol, size_to_close)
                if float(fmt_size) <= 0: return
                order = await self.exchange.create_market_order(self.symbol, close_side, fmt_size, {'reduceOnly': True})
                filled_order = await self.confirm_order_filled(order['id'])
                if not isinstance(filled_order, dict): logger.critical(f"部分平仓订单 {order['id']} 超时未确认！"); return
                closed_size, exit_price = filled_order.get('filled'), filled_order.get('average')
                if not all([isinstance(v, (int, float)) and v is not None and v > 0 for v in [closed_size, exit_price]]): self.position.handle_partial_close(closed_size or 0); return
                closing_fee = extract_fee(filled_order)
                prop_entry_fee = (pos['entry_fee'] / pos['size']) * closed_size if pos['size'] > 0 else 0.0
                gross_pnl = (exit_price - pos['entry_price']) * closed_size if pos['side'] == 'long' else (pos['entry_price'] - exit_price) * closed_size
                net_pnl = gross_pnl - prop_entry_fee - closing_fee
                trade_record = {"symbol": self.symbol, "side": pos['side'], "entry_price": pos['entry_price'], "exit_price": exit_price, "size": closed_size, "entry_timestamp": pos['entries'][0]['timestamp'] if pos.get('entries') else 0, "exit_timestamp": filled_order.get('timestamp', 0), "net_pnl": net_pnl, "reason": f"Partial Close: {reason}"}
                if hasattr(self, 'profit_tracker'): self.profit_tracker.record_trade(trade_record)
                self.position.handle_partial_close(closed_size)
                pnl_str = f"+{net_pnl:.2f}" if net_pnl >= 0 else f"{net_pnl:.2f}"

                if self.notifications_enabled:
                    send_bark_notification(f"原因: {reason}\n平掉数量: {fmt_size}\n本次净利: {pnl_str} USDT", f"🛡️ {self.symbol} 部分止盈")
        
        except (InsufficientFunds, ExchangeError, Exception) as e:
            if isinstance(e, InsufficientFunds): logger.critical(f"!!! 保证金不足 !!! 在执行({action}, {side})时发生严重错误。")
            elif isinstance(e, ccxt.ExchangeError): logger.error(f"交易所错误 ({type(e).__name__}) 在执行({action}, {side})时: {e}")
            else: logger.error(f"执行交易({action}, {side})时发生未知错误: {type(e).__name__}: {e}", exc_info=True)

    async def _apply_defensive_stop_loss(self, current_price: float):
        atr = await self.get_atr_data(period=14)
        if atr:
            pos = self.position.get_status()
            new_stop_loss = current_price - (atr * futures_settings.TREND_EXIT_ATR_MULTIPLIER) if pos['side'] == 'long' else current_price + (atr * futures_settings.TREND_EXIT_ATR_MULTIPLIER)
            if self.position.update_stop_loss(new_stop_loss, reason="Defensive Adjustment"):
                self.logger.info(f"防御性止损已更新至: {new_stop_loss:.4f}")
        else: self.logger.error("防御性止损失败：无法获取ATR数据。")

    async def _handle_trend_disagreement(self, current_trend: str, current_price: float):
        if not futures_settings.TREND_EXIT_ADJUST_SL_ENABLED or not self.position.is_position_open(): return
        pos = self.position.get_status()
        initial_risk = pos.get('initial_risk_per_unit', 0.0)
        profit_multiple = 0.0
        if initial_risk > 0: profit_multiple = ((current_price - pos['entry_price']) if pos['side'] == 'long' else (pos['entry_price'] - current_price)) / initial_risk
        if profit_multiple < 0: self.position.reset_partial_tp_counter(reason="利润转为负数")
        is_disagreement = (pos['side'] == 'long' and current_trend != 'uptrend') or (pos['side'] == 'short' and current_trend != 'downtrend')
        if is_disagreement: self.trend_exit_counter += 1
        elif self.trend_exit_counter > 0: self.trend_exit_counter = 0; return
        if self.trend_exit_counter >= futures_settings.TREND_EXIT_CONFIRMATION_COUNT:
            if pos['partial_tp_counter'] < 1 and profit_multiple > 0:
                size_to_close = pos['size'] * 0.5
                await self.execute_trade('partial_close', size=size_to_close, reason="Trend Disagreement Partial TP")
                self.position.increment_partial_tp_counter()
                be_price = self.position.break_even_price
                if be_price is not None and be_price > 0: self.position.update_stop_loss(be_price, reason="Secure after Partial TP")
            else: await self._apply_defensive_stop_loss(current_price)
            self.trend_exit_counter = 0


    async def _check_ranging_signal(self, current_price: float, ohlcv_ranging: list):
        if self.position.is_position_open(): return None
        try:
            # --- [核心修改] 使用传入的 ohlcv_ranging (15分钟数据) ---
            bbands = await self.get_bollinger_bands_data(
                ohlcv_data=ohlcv_ranging, 
                period=settings.RANGING_BBANDS_PERIOD, 
                std_dev=settings.RANGING_BBANDS_STD_DEV
            )
            # --- 修改结束 ---

            if bbands is None: return None
            entry_side = None
            if current_price <= bbands['lower']: entry_side = 'long'
            elif current_price >= bbands['upper']: entry_side = 'short'

            if entry_side: 
                self.logger.warning(f"⚡️ 侦测到震荡交易信号 ({settings.RANGING_TIMEFRAME}): {entry_side.upper()} @ {current_price:.4f}")
                return entry_side
            else: 
                self.logger.info(f"等待震荡入场 ({settings.RANGING_TIMEFRAME}): 价格({current_price:.4f})在轨道内 ({bbands['lower']:.4f} - {bbands['upper']:.4f})。")
                return None
        except Exception as e:
            self.logger.error(f"检查震荡信号时出错: {e}", exc_info=True); return None

    async def _manage_ranging_position(self, current_price: float, ohlcv_ranging: list):
        pos = self.position.get_status()
        exit_reason = await self._check_exit_signal(current_price)
        if exit_reason: await self.execute_trade('close', reason=f"Ranging - {exit_reason}"); return
        
        # --- [核心修改] 使用传入的 ohlcv_ranging (15分钟数据) ---
        bbands = await self.get_bollinger_bands_data(
            ohlcv_data=ohlcv_ranging, 
            period=settings.RANGING_BBANDS_PERIOD, 
            std_dev=settings.RANGING_BBANDS_STD_DEV
        )
        # --- 修改结束 ---

        if bbands is None: return
        take_profit_price = 0.0
        if settings.RANGING_TAKE_PROFIT_TARGET == 'middle': 
            take_profit_price = bbands['middle']
        elif settings.RANGING_TAKE_PROFIT_TARGET == 'opposite': 
            take_profit_price = bbands['upper'] if pos['side'] == 'long' else bbands['lower']
            
        if take_profit_price > 0 and ((pos['side'] == 'long' and current_price >= take_profit_price) or (pos['side'] == 'short' and current_price <= take_profit_price)):
            self.logger.warning(f"✅ 震荡策略止盈 ({settings.RANGING_TIMEFRAME}): 价格({current_price:.4f})已达到目标({take_profit_price:.4f})。")
            await self.execute_trade('close', reason='Ranging Take Profit')

    
    async def _check_and_execute_pyramiding(self, current_price: float, current_trend: str):
        if not futures_settings.PYRAMIDING_ENABLED or not self.position.is_position_open(): return
        pos = self.position.get_status()
        if pos['add_count'] >= futures_settings.PYRAMIDING_MAX_ADD_COUNT or ((pos['side'] == 'long' and current_trend != 'uptrend') or (pos['side'] == 'short' and current_trend != 'downtrend')): return
        initial_risk = pos.get('initial_risk_per_unit', 0.0)
        if initial_risk == 0: return
        pnl_per_unit = current_price - pos['entries'][0]['price'] if pos['side'] == 'long' else pos['entries'][0]['price'] - current_price
        target_multiplier = self.dyn_pyramiding_trigger * (pos['add_count'] + 1)
        if pnl_per_unit < initial_risk * target_multiplier: return
        
        add_size = self.position.entries[-1]['size'] * futures_settings.PYRAMIDING_ADD_SIZE_RATIO
        
        if add_size < self.min_trade_amount:
            self.logger.warning(
                f"计算出的加仓数量 ({add_size:.8f}) 小于最小要求 ({self.min_trade_amount:.8f})。"
                f"将自动调整为最小允许数量进行加仓。"
            )
            add_size = self.min_trade_amount

        formatted_size = self.exchange.exchange.amount_to_precision(self.symbol, add_size)
        api_side = 'buy' if pos['side'] == 'long' else 'sell'
        try:
            order = await self.exchange.create_market_order(self.symbol, api_side, formatted_size)
            filled = await self.confirm_order_filled(order['id'])
            if not filled: return
            add_fee = extract_fee(filled)
            self.position.add_to_position(filled['average'], filled['filled'], add_fee, filled['timestamp'])
            new_pos = self.position.get_status()
            if new_pos['add_count'] == 2: self.position.reset_partial_tp_counter(reason="Second pyramiding add completed")

            if self.notifications_enabled:
                send_bark_notification(f"Avg Price: {new_pos['entry_price']:.4f}\nTotal Size: {new_pos['size']:.5f}", f"➕ {self.symbol} Pyramiding Add successful ({new_pos['add_count']})")
            
            atr = await self.get_atr_data(period=14)
            if atr:
                atr_sl = current_price - (atr * self.dyn_atr_multiplier) if new_pos['side'] == 'long' else current_price + (atr * self.dyn_atr_multiplier)
                be_price = self.position.break_even_price
                if be_price is not None and be_price > 0:
                    final_sl = max(be_price, atr_sl) if new_pos['side'] == 'long' else min(be_price, atr_sl) if atr_sl > 0 else be_price
                    self.position.update_stop_loss(final_sl, reason="Pyramiding Secure")
        except Exception as e:
            self.logger.error(f"Error during pyramiding execution: {e}", exc_info=True)


    async def _check_reversal_danger_signal(self, ohlcv_5m: list, ohlcv_15m: list) -> bool:
        if not futures_settings.ENABLE_REVERSAL_SIGNAL_ALERT or not self.position.is_position_open(): return False
        try:
            pos_side = self.position.get_status()['side']
            last_closed_candle = ohlcv_5m[-2]
            candle_open, _, _, candle_close, candle_volume = last_closed_candle[1:6]
            is_adverse_candle = (pos_side == 'long' and candle_close < candle_open) or (pos_side == 'short' and candle_close > candle_open)
            if not is_adverse_candle: return False
            atr = await self.get_atr_data(period=14, ohlcv_data=ohlcv_15m)
            if atr is None or atr == 0: return False
            body_size = abs(candle_close - candle_open)
            if body_size < atr * futures_settings.REVERSAL_ALERT_BODY_ATR_MULTIPLIER: return False
            df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            avg_volume = df_5m['volume'].iloc[-(settings.TREND_VOLUME_CONFIRM_PERIOD + 1):-1].mean()
            volume_threshold = avg_volume * futures_settings.REVERSAL_ALERT_VOLUME_MULTIPLE
            if candle_volume < volume_threshold: return False
            self.logger.critical(f"！！！持仓风险预警！！！侦测到强力反向K线 (量: {candle_volume:.0f} > {volume_threshold:.0f}, 实体: {body_size:.4f} > {atr * futures_settings.REVERSAL_ALERT_BODY_ATR_MULTIPLIER:.4f})")
            return True
        except Exception as e:
            self.logger.error(f"检查危险信号时出错: {e}", exc_info=True); return False


    async def _check_and_manage_trend_exhaustion(self, ohlcv_15m: list):
        """[V3 - 修复版] 检查趋势是否正在衰竭，并提前将止损移动到盈亏平衡点。"""
        self.last_exhaustion_analysis = {"status": "Monitoring", "adx_value": None, "is_falling": False}

        if not futures_settings.ENABLE_EXHAUSTION_ALERT or not self.position.is_position_open():
            self.last_exhaustion_analysis["status"] = "Not Active"
            return

        pos = self.position.get_status()
        if pos.get('sl_stage', 1) != 1:
            self.last_exhaustion_analysis["status"] = f"Inactive (SL Stage: {pos.get('sl_stage')})"
            return

        try:
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # --- [核心修复] 调用统一的、正确的ADX计算函数 ---
            adx_series = await self.get_adx_data(
                period=futures_settings.EXHAUSTION_ADX_PERIOD, 
                ohlcv_df=df_15m, 
                return_series=True
            )
            # --- 修复结束 ---

            if adx_series is None or adx_series.isnull().all(): return

            current_adx = adx_series.iloc[-1]
            self.last_exhaustion_analysis["adx_value"] = f"{current_adx:.2f}"
            
            falling_bars = futures_settings.EXHAUSTION_ADX_FALLING_BARS
            if len(adx_series) < falling_bars + 1: return

            last_n_adx = adx_series.iloc[-(falling_bars + 1):]
            adx_diff = last_n_adx.diff().dropna()

            is_falling = not adx_diff.empty and all(adx_diff < 0)
            is_above_threshold = last_n_adx.iloc[0] > futures_settings.EXHAUSTION_ADX_THRESHOLD
            self.last_exhaustion_analysis["is_falling"] = is_falling

            if is_above_threshold and is_falling:
                self.last_exhaustion_analysis["status"] = "🔥 Triggered!"
                self.logger.warning(f"🛡️ 趋势衰竭预警！ADX 从 {last_n_adx.iloc[0]:.2f} 连续回落。止损将移动至盈亏平衡点。")
                be_price = self.position.break_even_price
                if be_price > 0:
                    updated = self.position.update_stop_loss(be_price, reason="Move SL to Breakeven")
                    if updated:
                        self.position.advance_sl_stage(1.5) 
        except Exception as e:
            self.logger.error(f"检查趋势衰竭时出错: {e}", exc_info=True)
            self.last_exhaustion_analysis["status"] = "Error"


    async def main_loop(self):
        if not self.initialized: await self.initialize()
        while True:
            try:
                ma_requirement = max(settings.TREND_LONG_MA_PERIOD, 30) + 5
                trendline_requirement = settings.TRENDLINE_LOOKBACK_PERIOD + 5
                ohlcv_5m_limit = max(ma_requirement, trendline_requirement)
                ohlcv_15m_limit = max(settings.TREND_FILTER_MA_PERIOD + 50, futures_settings.EXHAUSTION_ADX_PERIOD * 3)
                
                ticker, ohlcv_5m, ohlcv_15m = await asyncio.gather(
                    self.exchange.fetch_ticker(self.symbol), 
                    self.exchange.fetch_ohlcv(self.symbol, '5m', ohlcv_5m_limit), 
                    self.exchange.fetch_ohlcv(self.symbol, '15m', ohlcv_15m_limit)
                )
                current_price = ticker['last']

                if not all([current_price, ohlcv_5m, ohlcv_15m]): 
                    await asyncio.sleep(10); continue

                current_trend = await self._detect_trend(ohlcv_5m, ohlcv_15m)

                if not self.position.is_position_open():
                    await self._check_spike_entry_signal(ohlcv_5m, ohlcv_15m)
                    if settings.ENABLE_RANGING_STRATEGY and current_trend == 'sideways':
                        entry_side = await self._check_ranging_signal(current_price, ohlcv_15m)
                        if entry_side: await self.execute_trade('open', side=entry_side, reason='ranging_entry')
                    elif current_trend in ['uptrend', 'downtrend']:
                        trade_executed = False
                        breakout_result = await self._check_breakout_signal(ohlcv_5m, ohlcv_15m)
                        if isinstance(breakout_result, tuple):
                            await self.execute_trade('open', side=breakout_result[1], reason='breakout_momentum_trade'); trade_executed = True
                        if not trade_executed:
                            entry_side = await self._check_entry_signal(current_trend, current_price, ohlcv_5m, ohlcv_15m)
                            if entry_side: await self.execute_trade('open', side=entry_side, reason='pullback_entry')
                else:
                    pos_status = self.position.get_status()
                    is_danger_signal = await self._check_reversal_danger_signal(ohlcv_5m, ohlcv_15m)
                    if is_danger_signal:
                        self.logger.warning("因危险信号，立即收紧止损进入防御模式！")
                        await self._apply_defensive_stop_loss(current_price)
                    
                    if pos_status.get('entry_reason') == 'ranging_entry':
                        await self._manage_ranging_position(current_price, ohlcv_15m)
                    else:
                        trend_for_manage = await self._detect_trend(ohlcv_5m, ohlcv_15m)
                        await self._check_and_manage_trend_exhaustion(ohlcv_15m)
                        if pos_status.get('entry_reason') == 'breakout_momentum_trade':
                            await self._manage_breakout_momentum_stop(current_price)
                        else:
                            if not is_danger_signal:
                                await self._check_and_execute_pyramiding(current_price, trend_for_manage)
                            await self._handle_trend_disagreement(trend_for_manage, current_price)
                            await self._update_trailing_stop(current_price, trend_for_manage, ohlcv_5m, ohlcv_15m)
                        exit_reason = await self._check_exit_signal(current_price)
                        if exit_reason: await self.execute_trade('close', reason=exit_reason)
                
                current_time = time.time()
                if current_time - self.last_status_log_time >= 60:
                    current_trend_for_log = await self._detect_trend(ohlcv_5m, ohlcv_15m)
                    filter_ma_value = "N/A"
                    if len(ohlcv_15m) >= settings.TREND_FILTER_MA_PERIOD:
                        ohlcv_15m_df = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        filter_ma_series = ohlcv_15m_df['close'].ewm(span=settings.TREND_FILTER_MA_PERIOD, adjust=False).mean()
                        if not filter_ma_series.empty: filter_ma_value = filter_ma_series.iloc[-1]
                    await self._log_status_snapshot(current_price, current_trend_for_log, filter_ma_value, ohlcv_15m=ohlcv_15m)
                    self.last_status_log_time = current_time
                
                await self._sync_funding_fees()
                await asyncio.sleep(10)
            except Exception as e:
                self.logger.critical(f"主循环发生致命错误: {e}", exc_info=True)
                await asyncio.sleep(60)


