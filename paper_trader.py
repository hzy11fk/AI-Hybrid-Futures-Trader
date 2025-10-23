import asyncio
import logging
import time
import pandas as pd
from datetime import datetime
import ccxt.async_support as ccxt
import numpy as np

# 模拟真实的FuturesTrendTrader，但剥离了所有真实的交易执行
from futures_trader import FuturesTrendTrader 
# 模拟真实的交易所客户端
from exchange_client import ExchangeClient
# 加载您的所有配置
from config import settings, futures_settings
from helpers import setup_logging

def generate_performance_report(trade_history: list, initial_balance: float):
    """根据交易历史记录生成一份详细的性能报告"""
    if not trade_history:
        print("\n--- 策略性能报告 ---")
        print("未发生任何交易，无法生成报告。")
        return

    df = pd.DataFrame(trade_history)
    
    # 核心指标计算
    total_trades = len(df)
    winning_trades = df[df['net_pnl'] > 0]
    losing_trades = df[df['net_pnl'] <= 0]
    
    win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
    
    total_net_profit = df['net_pnl'].sum()
    
    average_profit = winning_trades['net_pnl'].mean() if len(winning_trades) > 0 else 0
    average_loss = abs(losing_trades['net_pnl'].mean()) if len(losing_trades) > 0 else 0
    
    payoff_ratio = average_profit / average_loss if average_loss > 0 else float('inf')
    
    profit_factor = winning_trades['net_pnl'].sum() / abs(losing_trades['net_pnl'].sum()) if abs(losing_trades['net_pnl'].sum()) > 0 else float('inf')

    # 计算权益曲线和最大回撤
    df['cumulative_pnl'] = df['net_pnl'].cumsum()
    df['equity_curve'] = initial_balance + df['cumulative_pnl']
    df['peak'] = df['equity_curve'].cummax()
    df['drawdown'] = (df['peak'] - df['equity_curve']) / df['peak']
    max_drawdown = df['drawdown'].max() * 100

    # 打印报告
    print("\n" + "="*50)
    print("--- 策略性能报告 (前瞻性测试) ---")
    print("="*50)
    print(f"测试周期: {pd.to_datetime(df['exit_timestamp'].iloc[0]).strftime('%Y-%m-%d')} to {pd.to_datetime(df['exit_timestamp'].iloc[-1]).strftime('%Y-%m-%d')}")
    print(f"初始资金: ${initial_balance:,.2f}")
    print(f"最终权益: ${df['equity_curve'].iloc[-1]:,.2f}")
    print("-" * 50)
    print(f"总净利润   : ${total_net_profit:,.2f}")
    print(f"总交易次数 : {total_trades} 次")
    print(f"胜率       : {win_rate:.2f}%")
    print(f"盈亏比     : {payoff_ratio:.2f} : 1")
    print(f"盈利因子   : {profit_factor:.2f}")
    print(f"最大回撤   : {max_drawdown:.2f}%")
    print("-" * 50)
    print(f"平均盈利   : ${average_profit:,.2f}")
    print(f"平均亏损   : ${average_loss:,.2f}")
    print("="*50 + "\n")


class MockExchange:
    """
    一个高度逼真的模拟交易所类。
    能处理首次开仓、加仓、部分平仓和完全平仓。
    """
    def __init__(self, initial_balance=1000.0, fee_rate=0.0005):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.used_margin = 0.0
        self.positions = {}
        self.fee_rate = fee_rate
        self.trade_history = []
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info(f"模拟交易所已初始化。初始资金: ${initial_balance:.2f}, 手续费率: {fee_rate * 100:.4f}%")
    def get_total_equity(self, current_prices: dict):
        unrealized_pnl = 0.0
        for symbol, pos in self.positions.items():
            if pos.get('is_open'):
                price = current_prices.get(symbol, pos['entry_price'])
                pnl = (price - pos['entry_price']) * pos['size'] if pos['side'] == 'long' else (pos['entry_price'] - price) * pos['size']
                unrealized_pnl += pnl
        return self.balance + unrealized_pnl

    def get_available_balance(self):
        return self.balance - self.used_margin

    def execute_order(self, symbol, side, size, price, leverage):
        if size <= 0:
            self.logger.error(f"[{symbol}] 订单数量错误: {size}"); return None

        pos = self.positions.get(symbol, {})
        is_position_open = pos.get('is_open', False)
        order_side_is_long = side == 'buy'

        # --- 逻辑分支 ---
        # 1. 加仓
        if is_position_open and ((order_side_is_long and pos['side'] == 'long') or (not order_side_is_long and pos['side'] == 'short')):
            self.logger.info(f"[{symbol}] 收到加仓指令...")
            margin_required = (size * price) / leverage
            if self.get_available_balance() < margin_required:
                self.logger.critical(f"[{symbol}] 加仓保证金不足！需要: ${margin_required:.2f}, 可用: ${self.get_available_balance():.2f}")
                return None
            
            fee = size * price * self.fee_rate
            self.balance -= fee
            self.used_margin += margin_required
            
            old_size, old_price = pos['size'], pos['entry_price']
            new_total_size = old_size + size
            new_avg_price = ((old_size * old_price) + (size * price)) / new_total_size
            
            pos['entry_price'] = new_avg_price
            pos['size'] = new_total_size
            pos['margin'] += margin_required
            pos['entry_fee'] += fee
            
            self.logger.warning(f"➕ [{symbol}] 模拟加仓成功 | 新均价: {new_avg_price:.4f}, 新数量: {new_total_size:.5f}")
            return {'filled': size, 'average': price, 'timestamp': time.time() * 1000}

        # 2. 平仓 (部分或全部)
        elif is_position_open and ((not order_side_is_long and pos['side'] == 'long') or (order_side_is_long and pos['side'] == 'short')):
            self.logger.info(f"[{symbol}] 收到平仓指令...")
            closed_size = min(size, pos['size'])
            is_full_close = abs(closed_size - pos['size']) < 1e-9

            pnl = (price - pos['entry_price']) * closed_size if pos['side'] == 'long' else (pos['entry_price'] - price) * closed_size
            
            prop_entry_fee = (pos['entry_fee'] / pos['size']) * closed_size if pos['size'] > 0 else 0
            exit_fee = closed_size * price * self.fee_rate
            net_pnl = pnl - prop_entry_fee - exit_fee
            
            self.balance += net_pnl
            
            released_margin = (closed_size / pos['size']) * pos['margin'] if pos['size'] > 0 else pos['margin']
            self.used_margin -= released_margin

            trade_record = {"symbol": symbol, "side": pos['side'], "entry_price": pos['entry_price'], "exit_price": price, 
                            "size": closed_size, "net_pnl": net_pnl, "reason": "partial_close" if not is_full_close else "full_close",
                            "exit_timestamp": datetime.now().isoformat()}
            self.trade_history.append(trade_record)
            
            pnl_str = f"+{net_pnl:.2f}" if net_pnl >= 0 else f"{net_pnl:.2f}"
            
            if is_full_close:
                pos['is_open'] = False
                self.logger.warning(f"💰 [{symbol}] 模拟完全平仓成功 | 净利润: {pnl_str} USDT | 新余额: ${self.balance:.2f}")
            else:
                pos['size'] -= closed_size
                pos['margin'] -= released_margin
                pos['entry_fee'] -= prop_entry_fee
                self.logger.warning(f"🛡️ [{symbol}] 模拟部分平仓成功 | 平掉数量: {closed_size:.5f}, 本次净利: {pnl_str} USDT")

            return {'filled': closed_size, 'average': price, 'timestamp': time.time() * 1000}

        # 3. 首次开仓
        else:
            self.logger.info(f"[{symbol}] 收到首次开仓指令...")
            margin_required = (size * price) / leverage
            if self.get_available_balance() < margin_required:
                self.logger.critical(f"[{symbol}] 可用保证金不足！需要: ${margin_required:.2f}, 可用: ${self.get_available_balance():.2f}")
                return None
            
            self.used_margin += margin_required
            fee = size * price * self.fee_rate
            self.balance -= fee
            
            self.positions[symbol] = {
                'is_open': True, 'side': 'long' if order_side_is_long else 'short', 'entry_price': price,
                'size': size, 'margin': margin_required, 'entry_timestamp': datetime.now().isoformat(), 'entry_fee': fee
            }
            self.logger.warning(f"✅ [{symbol}] 模拟开仓成功 | 方向: {self.positions[symbol]['side'].upper()}, 价格: {price:.4f}, 数量: {size:.5f}")
            return {'filled': size, 'average': price, 'timestamp': time.time() * 1000}


class PaperTrader(FuturesTrendTrader):
    """
    一个用于纸上交易的策略执行器。
    它继承了所有策略逻辑，但重写了交易执行和初始化部分。
    """
    def __init__(self, exchange_client, symbol: str, mock_exchange: MockExchange):
        super().__init__(exchange_client, symbol)
        self.mock_exchange = mock_exchange
        self.logger.warning("PaperTrader已初始化，所有交易将在本地模拟执行。")
        self.notifications_enabled = False
        self.logger.info("Bark通知已为模拟交易禁用。")
    async def initialize(self):
        """
        为纸上交易重写的、更安全的初始化方法。
        它只执行读取市场信息的操作，跳过了设置杠杆和保证金模式。
        """
        try:
            await self.exchange.load_markets()
            market_info = self.exchange.exchange.market(self.symbol)
            
            self.min_trade_amount = market_info.get('limits', {}).get('amount', {}).get('min', 0.001)
            if self.min_trade_amount is None or self.min_trade_amount == 0.0: self.min_trade_amount = 0.001
            self.taker_fee_rate = market_info.get('taker', self.taker_fee_rate)
            
            self.logger.info(f"[{self.symbol}] 纸上交易初始化：已加载市场信息。最小交易量: {self.min_trade_amount}")
            self.logger.info(f"[{self.symbol}] 跳过设置杠杆和保证金模式（仅模拟）。")
            
            if self.profit_tracker.is_new:
                 self.logger.info(f"[{self.symbol}] 利润账本为新，在模拟模式下从零开始。")

            self.initialized = True
        except Exception as e:
            self.logger.error(f"纸上交易初始化失败: {e}", exc_info=True)
            self.initialized = False

    async def execute_trade(self, action: str, side: str = None, reason: str = '', size: float = None):
        logger = self.logger
        if action == 'open' and side:
            try:
                ticker = await self.exchange.fetch_ticker(self.symbol)
                entry_price = ticker['last']
                if not isinstance(entry_price, (int, float)) or entry_price <= 0: logger.error(f"获取价格无效 ({entry_price})，取消开仓。"); return
                
                current_prices = {self.symbol: entry_price}
                total_equity = self.mock_exchange.get_total_equity(current_prices)
                if total_equity <= 0: logger.critical("模拟账户权益为0，无法开仓。"); return
                leverage = futures_settings.FUTURES_LEVERAGE
                min_notional = getattr(futures_settings, 'MIN_NOMINAL_VALUE_USDT', 21.0)
                price_diff_per_unit = 0.0

                if reason == 'ranging_entry':
                    ohlcv_5m = await self.exchange.fetch_ohlcv(self.symbol, '5m', 150)
                    atr = await self.get_atr_data(period=14, ohlcv_data=ohlcv_5m)
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
                    logger.critical(f"！！！开仓保证金校验失败！！！所需保证金 ({required_margin:.2f}) > 上限 ({max_allowed_margin:.2f})。取消本次开仓。")
                    return
                
                if final_pos_size <= 0: logger.error(f"计算仓位为0或负数({final_pos_size})，取消开仓。"); return
                
                api_side = 'buy' if side == 'long' else 'sell'
                execution_result = self.mock_exchange.execute_order(self.symbol, api_side, final_pos_size, entry_price, leverage)

                if execution_result and self.mock_exchange.positions.get(self.symbol, {}).get('is_open'):
                    pos = self.mock_exchange.positions[self.symbol]
                    sl_price = pos['entry_price'] - price_diff_per_unit if pos['side'] == 'long' else pos['entry_price'] + price_diff_per_unit
                    self.position.open_position(pos['side'], pos['entry_price'], pos['size'], pos['entry_fee'], sl_price, 0.0, time.time() * 1000, reason)
            except Exception as e:
                logger.error(f"模拟开仓时发生错误: {e}", exc_info=True)

        elif action == 'close':
            if not self.position.is_position_open(): return
            pos_status = self.position.get_status()
            api_side = 'sell' if pos_status['side'] == 'long' else 'buy'
            ticker = await self.exchange.fetch_ticker(self.symbol)
            self.mock_exchange.execute_order(self.symbol, api_side, pos_status['size'], ticker['last'], futures_settings.FUTURES_LEVERAGE)
            self.position.close_position()
            
        elif action == 'partial_close':
            if not self.position.is_position_open() or size is None or size <= 0: return
            pos = self.position.get_status()
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            size_to_close = min(size, pos['size'])
            if size_to_close <= 0: return
            
            ticker = await self.exchange.fetch_ticker(self.symbol)
            filled = self.mock_exchange.execute_order(self.symbol, close_side, size_to_close, ticker['last'], futures_settings.FUTURES_LEVERAGE)
            if filled:
                self.position.handle_partial_close(filled['filled'])

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
            self.logger.warning(f"计算出的加仓数量 ({add_size:.8f}) 小于最小要求 ({self.min_trade_amount:.8f})。将自动调整为最小允许数量进行加仓。")
            add_size = self.min_trade_amount
        
        api_side = 'buy' if pos['side'] == 'long' else 'sell'
        try:
            filled = self.mock_exchange.execute_order(self.symbol, api_side, add_size, current_price, futures_settings.FUTURES_LEVERAGE)
            if not filled: return
            
            add_fee = filled['filled'] * filled['average'] * self.mock_exchange.fee_rate
            self.position.add_to_position(filled['average'], filled['filled'], add_fee, filled['timestamp'])
            new_pos = self.position.get_status()
            if new_pos['add_count'] == 2: self.position.reset_partial_tp_counter(reason="Second pyramiding add completed")
            
            atr = await self.get_atr_data(period=14)
            if atr:
                atr_sl = current_price - (atr * self.dyn_atr_multiplier) if new_pos['side'] == 'long' else current_price + (atr * self.dyn_atr_multiplier)
                be_price = self.position.break_even_price
                if be_price is not None and be_price > 0:
                    final_sl = max(be_price, atr_sl) if new_pos['side'] == 'long' else min(be_price, atr_sl) if atr_sl > 0 else be_price
                    self.position.update_stop_loss(final_sl, reason="Pyramiding Secure")
        except Exception as e:
            self.logger.error(f"模拟加仓时发生错误: {e}", exc_info=True)

async def main(mock_exchange: MockExchange):
    """
    主函数的新版本。
    它现在会一直运行，直到被外部中断。
    """
    setup_logging()
    logging.info("--- 启动纸上交易 (前瞻性测试) ---")

    api_key = settings.BINANCE_TESTNET_API_KEY if settings.USE_TESTNET else settings.BINANCE_API_KEY
    secret_key = settings.BINANCE_TESTNET_SECRET_KEY if settings.USE_TESTNET else settings.BINANCE_SECRET_KEY
    if not api_key or not secret_key:
        logging.critical("API Key或Secret Key未在.env文件中设置！")
        return

    exchange_instance = ccxt.binance({'apiKey': api_key, 'secret': secret_key, 'options': {'defaultType': 'swap'}})
    if settings.USE_TESTNET:
        exchange_instance.set_sandbox_mode(True)
        logging.warning("--- 正在使用币安测试网 ---")
    
    exchange_client = ExchangeClient(exchange=exchange_instance)
    await exchange_client.load_markets()
    
    # mock_exchange 实例从外部传入
    traders = [PaperTrader(exchange_client, symbol, mock_exchange) for symbol in settings.FUTURES_SYMBOLS_LIST]
    
    await asyncio.gather(*[trader.initialize() for trader in traders])
    
    # --- [核心修复] ---
    # 不再返回，而是持续等待所有main_loop任务运行
    # 因为main_loop是无限循环，所以这里会永远等待，直到被Ctrl+C中断
    await asyncio.gather(*[trader.main_loop() for trader in traders])


# 替换现有的 if __name__ == "__main__": 代码块
if __name__ == "__main__":
    # 在主程序块中创建 mock_exchange 实例
    mock_exchange_instance = MockExchange(initial_balance=settings.FUTURES_INITIAL_PRINCIPAL)
    try:
        # 将实例传递给 main 函数
        asyncio.run(main(mock_exchange_instance))
    except KeyboardInterrupt:
        logging.info("--- 纸上交易已手动停止 ---")
    except Exception as e:
        logging.critical(f"主程序发生致命错误: {e}", exc_info=True)
    finally:
        # 在程序结束时，使用这里的实例来生成报告
        if mock_exchange_instance:
            generate_performance_report(
                mock_exchange_instance.trade_history,
                mock_exchange_instance.initial_balance
            )
