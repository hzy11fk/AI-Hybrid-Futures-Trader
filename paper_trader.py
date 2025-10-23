import asyncio
import logging
import time
import pandas as pd
from datetime import datetime
import ccxt.async_support as ccxt
import numpy as np
from ai_analyzer import AIAnalyzer

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
    
    # [修复] 确保 exit_timestamp 是 pd.Timestamp 类型
    df['exit_timestamp'] = pd.to_datetime(df['exit_timestamp'])
    print(f"测试周期: {df['exit_timestamp'].iloc[0].strftime('%Y-%m-%d')} to {df['exit_timestamp'].iloc[-1].strftime('%Y-%m-%d')}")
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
    [修改] 增加了限价单逻辑。
    """
    def __init__(self, exchange_client: ExchangeClient, initial_balance=1000.0, fee_rate=0.0005):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.used_margin = 0.0
        self.positions = {}
        self.fee_rate = fee_rate
        self.trade_history = []
        self.logger = logging.getLogger(self.__class__.__name__)
        # [新增] 模拟交易所也需要访问真实K线
        self.real_exchange_client = exchange_client 
        
        # [新增] 模拟挂单
        self.pending_orders = {}
        self.order_id_counter = 1
        
        self.logger.info(f"模拟交易所已初始化。初始资金: ${initial_balance:.2f}, 手续费率: {fee_rate * 100:.4f}%")
        
    async def get_current_price(self, symbol):
        # 辅助函数，用于获取当前市价
        try:
            ticker = await self.real_exchange_client.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            self.logger.error(f"模拟交易所无法获取 {symbol} 的真实市价: {e}")
            return None

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

    def get_balance_snapshot(self):
        # 模拟 fetch_balance 的返回结构
        return {
            'total': {'USDT': self.get_total_equity({})}, # 注意：这里无法获取实时价格，所以股权可能不准
            'free': {'USDT': self.get_available_balance()}
        }

    # [修改] execute_order 重命名为 _execute_trade，并且只在内部调用
    def _execute_trade(self, symbol, side, size, price, leverage, order_id=None):
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
            return {'id': order_id, 'filled': size, 'average': price, 'timestamp': time.time() * 1000, 'status': 'closed'}

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
                pos['size'] = 0
                pos['margin'] = 0
                pos['entry_fee'] = 0
                self.logger.warning(f"💰 [{symbol}] 模拟完全平仓成功 | 净利润: {pnl_str} USDT | 新余额: ${self.balance:.2f}")
            else:
                pos['size'] -= closed_size
                pos['margin'] -= released_margin
                pos['entry_fee'] -= prop_entry_fee
                self.logger.warning(f"🛡️ [{symbol}] 模拟部分平仓成功 | 平掉数量: {closed_size:.5f}, 本次净利: {pnl_str} USDT")

            return {'id': order_id, 'filled': closed_size, 'average': price, 'timestamp': time.time() * 1000, 'status': 'closed'}

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
            return {'id': order_id, 'filled': size, 'average': price, 'timestamp': time.time() * 1000, 'status': 'closed'}
    
    # --- [新增] 模拟交易所的API接口 ---
    
    async def create_market_order(self, symbol, side, size):
        price = await self.get_current_price(symbol)
        if price is None:
            raise Exception(f"模拟市价单失败：无法获取 {symbol} 的价格")
        
        return self._execute_trade(symbol, side, size, price, futures_settings.FUTURES_LEVERAGE)

    async def create_limit_order(self, symbol, side, size, price):
        order_id = str(self.order_id_counter)
        self.order_id_counter += 1
        
        order = {
            'id': order_id, 'symbol': symbol, 'side': side, 'size': size, 'price': price,
            'status': 'open', 'timestamp': time.time() * 1000
        }
        self.pending_orders[order_id] = order
        self.logger.info(f"[{symbol}] 模拟限价单已提交: {side} {size} @ {price} (ID: {order_id})")
        return order

    async def fetch_order(self, order_id, symbol):
        if order_id not in self.pending_orders:
            # 也许是已成交的市价单？为简单起见，我们假设 fetch_order 只查限价单
            # 或者它已经被成交并移除了
            return {'id': order_id, 'status': 'closed'} 
            
        order = self.pending_orders[order_id]
        
        # --- 模拟限价单成交逻辑 ---
        current_price = await self.get_current_price(symbol)
        if current_price is None:
            return order # 无法获取价格，返回 'open' 状态
            
        is_filled = False
        if order['side'] == 'buy' and current_price <= order['price']:
            is_filled = True
        elif order['side'] == 'sell' and current_price >= order['price']:
            is_filled = True
            
        if is_filled:
            self.logger.warning(f"[{symbol}] 模拟限价单 {order_id} 成交！")
            del self.pending_orders[order_id]
            # 使用挂单价成交
            return self._execute_trade(symbol, order['side'], order['size'], order['price'], futures_settings.FUTURES_LEVERAGE, order_id)
        
        return order # 未成交，返回 'open' 状态
        
    async def cancel_order(self, order_id, symbol):
        if order_id in self.pending_orders:
            del self.pending_orders[order_id]
            self.logger.info(f"[{symbol}] 模拟订单 {order_id} 已取消。")
            return {'id': order_id, 'status': 'canceled'}
        return {'id': order_id, 'status': 'closed'} # 假设它已经被成交了


class PaperTrader(FuturesTrendTrader):
    """
    一个用于纸上交易的策略执行器。
    它继承了所有策略逻辑，但重写了与交易所的 *直接交互* 方法。
    """
    def __init__(self, exchange_client: ExchangeClient, symbol: str, mock_exchange: MockExchange):
        # [修改] 传入的 exchange_client 是 *真实* 的，用于获取K线
        super().__init__(exchange_client, symbol) 
        
        self.mock_exchange = mock_exchange
        self.logger.warning(f"[{self.symbol}] PaperTrader已初始化，所有交易将在本地模拟执行。")
        self.notifications_enabled = False
        self.logger.info(f"[{self.symbol}] Bark通知已为模拟交易禁用。")
        
        # --- [核心修改] ---
        # 重写父类的 exchange *实例*，将其替换为 PaperTrader 自身。
        # 这样当父类调用 self.exchange.create_market_order 时，
        # 它实际上会调用 PaperTrader.create_market_order
        self.exchange = self 

    async def initialize(self):
        """
        为纸上交易重写的、更安全的初始化方法。
        它只执行读取市场信息的操作，跳过了设置杠杆和保证金模式。
        """
        try:
            # [修改] 使用父类的真实 exchange 客户端 (self.exchange) 来加载市场
            # 注意：在 __init__ 中 self.exchange 已被重写为 self
            # 我们需要访问原始的 exchange_client
            original_exchange_client = super().exchange
            
            await original_exchange_client.load_markets()
            market_info = original_exchange_client.exchange.market(self.symbol)
            
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

    # --- [核心修改] 重写 ExchangeClient 的方法 ---
    # 我们不再重写 execute_trade，而是重写 execute_trade 所依赖的底层API
    
    async def fetch_balance(self, params={}):
        """重写：返回模拟余额"""
        self.logger.debug("调用模拟 fetch_balance")
        # 包装在 await 中以匹配异步签名
        return self.mock_exchange.get_balance_snapshot()

    async def create_market_order(self, symbol: str, side: str, amount: float, params={}):
        """重写：调用模拟市价单"""
        self.logger.debug(f"调用模拟 create_market_order: {side} {amount}")
        return await self.mock_exchange.create_market_order(symbol, side, amount)

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float, params={}):
        """重写：调用模拟限价单"""
        self.logger.debug(f"调用模拟 create_limit_order: {side} {amount} @ {price}")
        return await self.mock_exchange.create_limit_order(symbol, side, amount, price)

    async def fetch_order(self, order_id: str, symbol: str):
        """重写：调用模拟获取订单"""
        self.logger.debug(f"调用模拟 fetch_order: {order_id}")
        return await self.mock_exchange.fetch_order(order_id, symbol)

    async def cancel_order(self, order_id: str, symbol: str):
        """重写：调用模拟取消订单"""
        self.logger.debug(f"调用模拟 cancel_order: {order_id}")
        return await self.mock_exchange.cancel_order(order_id, symbol)

    async def confirm_order_filled(self, order_id, timeout=60, interval=2):
        """
        重写：模拟订单确认。
        市价单立即返回 'closed'，限价单依赖 fetch_order 逻辑。
        """
        self.logger.debug(f"调用模拟 confirm_order_filled: {order_id}")
        
        # 模拟市价单（它们没有 order_id 记录在 pending_orders 中）
        if order_id is None or order_id not in self.mock_exchange.pending_orders:
             # 假设这是一个已执行的市价单
             # 我们需要找到这笔交易... 但这很难。
             # 为简单起见，我们假设市价单总是成功的。
             # execute_trade 会处理 PositionTracker
             #
             # [!! 关键简化 !!] 真正的 `confirm_order_filled` 是在
             # `execute_trade` 内部调用的。在我们的模拟中，`create_market_order`
             # 已经 *同步* 执行了交易并返回了结果。
             #
             # `FuturesTrendTrader.execute_trade` 会收到这个 *已成交* 的结果，
             # 并尝试用它的 ID 调用 `confirm_order_filled`。
             
             # 我们返回一个模拟的已成交订单
             # TODO: 这部分逻辑需要改进，市价单也应该返回ID
             
             # 假设 `execute_trade` 拿到的 order['id'] 就是它
             # 并且 `create_market_order` 已返回成交结果
             
             # 在新的设计中，create_market_order 直接返回成交结果
             # `execute_trade` 拿到这个结果后，不应该再调用 `confirm_order_filled`
             # 啊，但是 `futures_trader.py` *会* 调用...
             
             # 让我们修改 `FuturesTrendTrader.execute_trade` 以适应模拟
             # 不，我们应该让模拟适应 `FuturesTrendTrader`
             
             # 当 `create_market_order` 被调用时，它返回一个 *已成交* 的 dict
             # `execute_trade` 拿到这个 dict，用 `order['id']` 调用 `confirm_order_filled`
             # `confirm_order_filled` 应该能识别这个 ID
             
             # 让我们假设 `_execute_trade` 返回的 dict 就是 `order`
             # 那么 `execute_trade` 拿到的 `order['id']` 可能是 None 或一个数字
             
             # 简便起见：在模拟模式下，市价单立即成交，
             # `confirm_order_filled` 直接返回传入的 order
             
             # 糟糕，`create_market_order` 返回的是 *成交后* 的 dict，
             # 而 `execute_trade` 期望的是 *刚创建* 的 dict。
             
             # 让我们回到 `MockExchange`
             # `create_market_order` 应该返回一个模拟的 "刚创建" 的订单
             # 但它内部已经执行了...
             
             # 算了，最简单的模拟：
             # `confirm_order_filled` 总是假设订单已成交
             # 它只需要调用 `fetch_order` 一次
             self.logger.warning(f"模拟 confirm_order_filled: 假设 {order_id} 已成交或正在检查")
             return await self.fetch_order(order_id, self.symbol)

    # --- [新增] 重写父类的只读方法，确保它们使用 *真实* 的交易所 ---
    
    @property
    def exchange(self):
        # 当父类访问 self.exchange 时 (例如 self.exchange.fetch_ticker)
        # 确保它访问的是 *原始* 的 exchange_client，而不是 PaperTrader 实例
        return super().exchange

    # --- [移除] 不再需要重写 execute_trade 或 _check_and_execute_pyramiding ---
    # 父类的原始逻辑将自动运行，并调用我们重写的 (create_market_order, etc.)


async def main(mock_exchange: MockExchange):
    """
    主函数的新版本。
    它现在会一直运行，直到被外部中断。
    """
    setup_logging()
    logging.info("--- 启动纸上交易 (前瞻性测试) ---")

    # [修改] 纸上交易也需要 API 密钥，用于 *读取* K线数据
    api_key = settings.BINANCE_TESTNET_API_KEY if settings.USE_TESTNET else settings.BINANCE_API_KEY
    secret_key = settings.BINANCE_TESTNET_SECRET_KEY if settings.USE_TESTNET else settings.BINANCE_SECRET_KEY
    if not api_key or not secret_key:
        logging.critical("API Key或Secret Key未在.env文件中设置！(纸上交易也需要它们来读取数据)")
        return

    exchange_instance = ccxt.binance({'apiKey': api_key, 'secret': secret_key, 'options': {'defaultType': 'swap'}})
    if settings.USE_TESTNET:
        exchange_instance.set_sandbox_mode(True)
        logging.warning("--- 正在使用币安测试网 ---")
    
    # 这是 *真实* 的交易所客户端，用于获取K线
    exchange_client = ExchangeClient(exchange=exchange_instance)
    await exchange_client.load_markets()
    
    # --- [核心新增逻辑] AI 连接预执行测试 ---
    if settings.ENABLE_AI_MODE:
        logging.info("执行 AI 服务连接预测试...")
        ai_tester = AIAnalyzer(exchange=exchange_client.exchange, symbol="CONNECTION_TEST")
        connection_ok = await ai_tester.test_connection()
        
        if not connection_ok:
            logging.critical("AI 连接测试未通过。程序将退出，请检查日志中的详细错误信息并修正配置。")
            await exchange_instance.close() 
            return 
    # --- 测试结束 ---
    
    # [修改] 将 *真实* 的 exchange_client 传递给 mock_exchange
    mock_exchange.real_exchange_client = exchange_client
    
    # [修改] PaperTrader 接收 *真实* 的 exchange_client (用于读)
    # 和 *模拟* 的 mock_exchange (用于写)
    traders = [PaperTrader(exchange_client, symbol, mock_exchange) for symbol in settings.FUTURES_SYMBOLS_LIST]
    
    await asyncio.gather(*[trader.initialize() for trader in traders])
    
    logging.info("--- 策略初始化完成，开始模拟 main_loop ---")
    
    await asyncio.gather(*[trader.main_loop() for trader in traders])
    
    # [新增] 关闭交易所连接
    await exchange_instance.close()


# 替换现有的 if __name__ == "__main__": 代码块
if __name__ == "__main__":
    # 在主程序块中创建 mock_exchange 实例
    # [修改] 构造函数现在需要一个 exchange_client，但我们此时还没有
    # 我们先传 None，然后在 main 函数中再设置它
    mock_exchange_instance = MockExchange(exchange_client=None, initial_balance=settings.FUTURES_INITIAL_PRINCIPAL)
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
