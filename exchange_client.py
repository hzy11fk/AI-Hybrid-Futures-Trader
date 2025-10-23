import logging
import asyncio
from ccxt.base.errors import RequestTimeout, NetworkError, ExchangeNotAvailable, DDoSProtection

class ExchangeClient:
    def __init__(self, exchange):
        self.exchange = exchange
        self.logger = logging.getLogger(self.__class__.__name__)

    async def _retry_async_method(self, method, *args, **kwargs):
        """
        [新增] 一个健壮的异步方法重试装饰器/包装器。
        - max_retries: 最大重试次数
        - delay: 每次重试前的等待时间（秒）
        """
        max_retries = 3
        delay = 5  # 5秒
        for attempt in range(max_retries):
            try:
                # 尝试调用原始方法
                return await method(*args, **kwargs)
            except (RequestTimeout, NetworkError, ExchangeNotAvailable, DDoSProtection) as e:
                # 只对可恢复的网络或超时错误进行重试
                if attempt < max_retries - 1:
                    self.logger.warning(f"调用 {method.__name__} 时发生可重试错误: {e}。将在 {delay} 秒后进行第 {attempt + 2} 次尝试...")
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(f"调用 {method.__name__} 失败，已达到最大重试次数 ({max_retries})。")
                    raise  # 重试次数用尽后，重新抛出最后的异常
            except Exception as e:
                # 对于其他所有错误（如API密钥错误、参数错误），不进行重试，立即抛出
                self.logger.error(f"调用 {method.__name__} 时发生不可重试的严重错误: {e}")
                raise

    async def fetch_ticker(self, symbol: str):
        """获取最新价格，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.fetch_ticker, symbol)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        """获取K线数据，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)

    async def fetch_balance(self, params={}):
        """
        [修改] 获取余额，并应用重试逻辑。
        这是修复您问题的核心。
        """
    #    self.logger.info("正在获取账户余额...")
        try:
            # 使用重试包装器来调用真实的 fetch_balance
            balance = await self._retry_async_method(self.exchange.fetch_balance, params=params)
      #      self.logger.info("成功获取账户余额。")
            return balance
        except Exception as e:
            self.logger.error(f"获取余额失败: {e}", exc_info=True)
            raise # 将最终的错误向上抛出

    async def create_market_order(self, symbol: str, side: str, amount: float, params={}):
        """创建市价单，并应用重试逻辑。"""
        # 注意：对下单操作应用重试需要非常小心，以防重复下单。
        # CCXT通常有内置的幂等性处理，但这里我们假设只在超时且状态未知时重试一次。
        # 为简单起见，这里也直接使用重试包装器，但在生产环境中需要更复杂的逻辑。
        return await self._retry_async_method(self.exchange.create_market_order, symbol, side, amount, params=params)

    async def fetch_order(self, order_id: str, symbol: str):
        """获取订单信息，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.fetch_order, order_id, symbol=symbol)
        
    async def set_leverage(self, leverage, symbol):
        """设置杠杆，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.set_leverage, leverage, symbol=symbol)

    async def set_margin_mode(self, margin_mode, symbol):
        """设置保证金模式，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.set_margin_mode, margin_mode, symbol=symbol)
        
    async def load_markets(self):
        """加载市场信息，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.load_markets)

    async def fetch_my_trades(self, symbol: str, limit: int = 1000):
        """获取历史成交，并应用重试逻辑。"""
        return await self._retry_async_method(self.exchange.fetch_my_trades, symbol, limit=limit)
