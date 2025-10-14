# 文件: main2.py (合约策略专用启动器)

import asyncio
from exchange_client import ExchangeClient
from config import settings
from helpers import setup_logging
import logging

# --- 确保这里导入的是我们全新的 FuturesTrendTrader 和 start_web_server ---
from futures_trader import FuturesTrendTrader
from web_server import start_web_server

async def main():
    setup_logging()
    logging.info("===== 启动合约趋势策略 (main2.py) =====")

    exchange_client = ExchangeClient(is_futures=True)

    traders = {}
    for symbol in settings.FUTURES_SYMBOLS_LIST:
        try:
            trader = FuturesTrendTrader(exchange_client, symbol)
            traders[symbol] = trader
        except Exception as e:
            logging.error(f"为合约交易对 {symbol} 创建实例失败: {e}", exc_info=True)

    if not traders:
        logging.critical("没有任何合约交易实例被成功创建，程序退出。")
        return

    # --- [核心] 创建一个任务列表，包含所有交易循环和Web服务 ---
    tasks = []
    for trader in traders.values():
        tasks.append(trader.main_loop())

    # 将traders字典传递给Web服务
    tasks.append(start_web_server(traders))

    logging.info("正在启动所有交易任务和Web服务...")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("合约策略程序被手动中断。")
