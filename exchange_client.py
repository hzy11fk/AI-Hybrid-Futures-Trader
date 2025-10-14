# 文件: exchange_client.py (最终修正版 - 补全函数)

import ccxt.async_support as ccxt
import logging
from config import settings
import asyncio

class ExchangeClient:
    def __init__(self, is_futures=False):
        """
        初始化交易所客户端。
        根据全局配置 settings.USE_TESTNET 自动切换实盘或测试网。
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.time_diff = 0
        
        if settings.USE_TESTNET:
            self.logger.warning("！！！当前正在使用币安测试网 (Testnet) 模式！！！")
            api_key = settings.BINANCE_TESTNET_API_KEY
            secret_key = settings.BINANCE_TESTNET_SECRET_KEY
            options = {'defaultType': 'future'}
            
            exchange_config = { 'apiKey': api_key, 'secret': secret_key, 'options': options, 'enableRateLimit': True }
            self.exchange = ccxt.binance(exchange_config)
            self.exchange.set_sandbox_mode(True) 
            
        else:
            self.logger.info("当前正在使用币安实盘 (Live) 模式。")
            api_key = settings.BINANCE_API_KEY
            secret_key = settings.BINANCE_SECRET_KEY
            options = {'defaultType': 'future'}
            
            exchange_config = { 'apiKey': api_key, 'secret': secret_key, 'options': options, 'enableRateLimit': True }
            self.exchange = ccxt.binance(exchange_config)

        self.markets_loaded = False

    async def load_markets(self, reload=False):
        """加载市场数据"""
        if not self.markets_loaded or reload:
            try:
                await self.exchange.load_markets(reload)
                self.markets_loaded = True
                self.logger.info("市场数据加载成功。")
            except Exception as e:
                self.logger.error(f"加载市场数据失败: {e}", exc_info=True)
                raise

    # --- [核心修正] 在此处添加缺失的 fetch_my_trades 函数 ---
    async def fetch_my_trades(self, symbol: str, limit: int = 1000):
        """获取我的历史成交记录"""
        try:
            return await self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e:
            self.logger.error(f"获取 {symbol} 历史成交失败: {e}", exc_info=True)
            raise
    # --- 修正结束 ---

    async def fetch_ticker(self, symbol: str):
        """获取最新价格"""
        try:
            return await self.exchange.fetch_ticker(symbol)
        except Exception as e:
            self.logger.error(f"获取 {symbol} Ticker失败: {e}", exc_info=True)
            raise

    async def fetch_balance(self, params={}):
        """获取账户余额"""
        try:
            return await self.exchange.fetch_balance(params=params)
        except Exception as e:
            self.logger.error(f"获取余额失败: {e}", exc_info=True)
            raise

    async def create_market_order(self, symbol: str, side: str, amount: float, params={}):
        """创建市价单"""
        try:
            self.logger.info(f"创建市价单: {side.upper()} {amount} {symbol} | 参数: {params}")
            return await self.exchange.create_market_order(symbol, side, amount, params)
        except Exception as e:
            self.logger.error(f"创建市价单失败: {e}", exc_info=True)
            raise

    async def fetch_order(self, order_id: str, symbol: str):
        """获取订单详情"""
        try:
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            self.logger.error(f"获取订单 {order_id} 失败: {e}", exc_info=True)
            raise

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1m', limit: int = 100):
        """获取K线数据"""
        try:
            return await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            self.logger.error(f"获取 {symbol} {timeframe} K线失败: {e}", exc_info=True)
            raise

    async def set_leverage(self, leverage: int, symbol: str):
        """设置杠杆倍数"""
        try:
            return await self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            self.logger.error(f"为 {symbol} 设置杠杆失败: {e}", exc_info=True)
            raise

    async def set_margin_mode(self, margin_mode: str, symbol: str):
        """设置保证金模式"""
        try:
            return await self.exchange.set_margin_mode(margin_mode, symbol)
        except Exception as e:
            self.logger.error(f"为 {symbol} 设置保证金模式失败: {e}", exc_info=True)
            raise

    async def close(self):
        """关闭交易所连接"""
        try:
            await self.exchange.close()
            self.logger.info("交易所连接已关闭。")
        except Exception as e:
            self.logger.error(f"关闭连接时出错: {e}", exc_info=True)
