# 文件: /root/heyue/main2.py

import asyncio
import ccxt.pro as ccxtpro # [新增] 导入 ccxt.pro
import logging
from config import settings
from exchange_client import ExchangeClient # 确保导入了 ExchangeClient
from futures_trader import FuturesTrendTrader
from web_server import start_web_server
from helpers import setup_logging # 假设您的 helpers.py 中有 setup_logging

async def main():
    setup_logging() # 初始化日志
    logger = logging.getLogger("Main")

    # --- [核心修改] 步骤 1: 创建真实的 ccxt 交易所对象 ---
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
        # 注意: ccxt.pro 使用 ccxtpro.binance()
        exchange = ccxtpro.binance(exchange_config)
        exchange.set_sandbox_mode(True)
    else:
        exchange = ccxtpro.binance(exchange_config)
    
    # --- [核心修改] 步骤 2: 将创建好的交易所对象传递给 ExchangeClient ---
    # exchange_client 现在是一个包装器，负责处理重试等逻辑
    exchange_client = ExchangeClient(exchange)

    # --- 后续逻辑保持不变 ---
    traders = {}
    for symbol in settings.FUTURES_SYMBOLS_LIST:
        # 将 exchange_client (而不是原始的 exchange) 传递给交易员
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
        # 此处可以添加取消 main_loops 任务的逻辑
    finally:
        await web_server_site.stop()
        await exchange.close()
        logger.info("所有服务已完全关闭。程序退出。")

if __name__ == "__main__":
    # 确保 main2.py 的主函数调用部分是正确的
    asyncio.run(main())
