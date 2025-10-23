# 文件: /root/heyue/main2.py (已修复 .env 加载问题)

# --- [核心修改] 步骤 1: 在所有其他导入之前，首先加载 .env 文件 ---
from dotenv import load_dotenv
load_dotenv()
# --- 修改结束 ---

import asyncio
import ccxt.pro as ccxtpro
import logging
from config import settings, futures_settings # 确保也导入了 futures_settings (如果需要)
from exchange_client import ExchangeClient
from futures_trader import FuturesTrendTrader
from web_server import start_web_server
from helpers import setup_logging

async def main():
    setup_logging() # 初始化日志
    logger = logging.getLogger("Main")

    # --- [核心修改] 步骤 2: 检查 AI_PROVIDER 是否已成功加载 ---
    # 增加一个启动时的检查，方便排查问题
    if getattr(settings, 'ENABLE_AI_MODE', False):
        if not getattr(settings, 'AI_PROVIDER', None):
            logger.critical("!!! 致命错误: AI模式已启用，但未能从配置文件中加载 AI_PROVIDER。请检查 .env 文件和加载顺序。")
            return
        logger.info(f"检测到 AI 服务商配置为: {settings.AI_PROVIDER.upper()}")
    # --- 修改结束 ---

    # 创建真实的 ccxt 交易所对象
    exchange_config = {
        'apiKey': settings.BINANCE_API_KEY,
        'secret': settings.BINANCE_SECRET_KEY,
        'options': {'defaultType': 'swap'}
    }
    if settings.USE_TESTNET:
        exchange_config.update({
            'apiKey': settings.BINANCE_TESTNET_API_KEY,
            'secret': settings.BINANCE_TESTNET_SECRET_KEY,
        })
        exchange = ccxtpro.binance(exchange_config)
        exchange.set_sandbox_mode(True)
    else:
        exchange = ccxtpro.binance(exchange_config)
    
    # 将创建好的交易所对象传递给 ExchangeClient
    exchange_client = ExchangeClient(exchange)

    traders = {}
    # 确保 FUTURES_SYMBOLS_LIST 是从 settings 中获取的
    for symbol in settings.FUTURES_SYMBOLS_LIST:
        trader = FuturesTrendTrader(exchange=exchange_client, symbol=symbol)
        traders[symbol] = trader

    init_tasks = [trader.initialize() for trader in traders.values()]
    await asyncio.gather(*init_tasks)

    active_traders = {sym: t for sym, t in traders.items() if t.initialized}
    
    if not active_traders:
        logger.error("所有交易员初始化失败，程序退出。")
        await exchange.close()
        return

    logger.info(f"成功初始化的交易对: {list(active_traders.keys())}")
    
    # 启动Web服务器
    web_server_site = await start_web_server(active_traders)
    
    # 启动所有交易员的主循环
    main_loops = [trader.main_loop() for trader in active_traders.values()]
    
    logger.warning("策略核心服务与Web监控页面均已启动。按 Ctrl+C 优雅地关闭程序。")
    
    try:
        await asyncio.gather(*main_loops)
    except KeyboardInterrupt:
        logger.warning("接收到关闭信号，正在优雅地关闭所有服务...")
    finally:
        await web_server_site.stop()
        await exchange.close()
        logger.info("所有服务已完全关闭。程序退出。")

if __name__ == "__main__":
    asyncio.run(main())
