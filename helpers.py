# 文件: helpers.py (最终完整版 - V4 兼容历史与实时数据)

import logging
from logging.handlers import TimedRotatingFileHandler
import os
import requests
import time

# --- 导入 settings 对象 ---
from config import settings


class LogConfig:
    """日志配置类"""
    LOG_DIR = 'logs'
    LOG_FILENAME = 'trading_system.log'
    BACKUP_DAYS = 7
    LOG_LEVEL = logging.INFO

    @staticmethod
    def setup_logger():
        """静态方法，用于设置全局日志记录器。"""
        logger = logging.getLogger()
        logger.setLevel(LogConfig.LOG_LEVEL)
        
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        if not os.path.exists(LogConfig.LOG_DIR):
            os.makedirs(LogConfig.LOG_DIR)
        
        file_handler = TimedRotatingFileHandler(
            os.path.join(LogConfig.LOG_DIR, LogConfig.LOG_FILENAME),
            when='midnight', interval=1, backupCount=LogConfig.BACKUP_DAYS, encoding='utf-8', delay=True
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(name)-20s] %(levelname)-8s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
        ))
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s: %(message)s'))
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

def setup_logging():
    """顶层函数，用于调用LogConfig类中的日志设置方法。"""
    LogConfig.setup_logger()
    logging.info("==================================================")
    logging.info("日志系统已初始化")
    logging.info("==================================================")


def send_bark_notification(content: str, title: str = "合约策略通知"):
    """[修正版] 发送通知到 Bark App，使用查询参数以避免特殊字符问题。"""
    if not settings.BARK_URL_KEY:
        logging.warning("未配置 BARK_URL_KEY，无法发送Bark通知。")
        return

    try:
        base_url = settings.BARK_URL_KEY
        params = {
            'title': title,
            'body': content,
            "copy": content
        }
        
        logging.info(f"正在发送Bark通知: {title}")
        response = requests.get(base_url, params=params, timeout=5)
        
        if response.status_code == 200:
            logging.info("Bark通知发送成功。")
        else:
            logging.error(f"Bark通知发送失败: 状态码={response.status_code}, 响应={response.text}")
            
    except Exception as e:
        logging.error(f"发送Bark通知时发生异常: {e}", exc_info=True)


# --- [核心优化] V4版手续费提取函数 ---
def extract_fee(order_or_trade: dict) -> float:
    """
    [V4 - 兼容版] 安全地从订单或成交对象中提取以USDT计价的总手续费。
    
    - 能够处理单个'fee'字典和'fees'列表两种格式。
    - **核心修复**: 如果检测到手续费币种为'BNB'，会智能地查找 'average'/'filled' (用于订单)
      或 'price'/'amount' (用于历史成交)，从而正确估算手续费。
    """
    logger = logging.getLogger("FeeExtractor")

    if not isinstance(order_or_trade, dict): 
        return 0.0
    
    def process_fee_logic(fee_data: dict, data_obj: dict) -> float:
        if not isinstance(fee_data, dict):
            return 0.0

        fee_cost = fee_data.get('cost')
        fee_currency = fee_data.get('currency')

        if fee_currency in ['USDT', 'BUSD']:
            return float(fee_cost) if fee_cost is not None else 0.0
        
        elif fee_currency == 'BNB':
            # --- [关键修改] ---
            # 首先尝试获取订单(Order)的字段，如果失败，则回退获取成交(Trade)的字段
            price = data_obj.get('average') or data_obj.get('price')
            amount = data_obj.get('filled') or data_obj.get('amount')
            # --- 修改结束 ---

            if not (isinstance(price, (int, float)) and price > 0 and 
                    isinstance(amount, (int, float)) and amount > 0):
                logger.error(
                    f"无法为BNB手续费估算名义价值，因为订单/成交记录缺少有效的价格或数量字段。Order/Trade ID: {data_obj.get('id')}"
                )
                return 0.0

            notional_value = price * amount
            estimated_usdt_fee = notional_value * 0.00045

            logger.warning(
                f"检测到BNB手续费！正在根据名义价值 ${notional_value:.2f} 和 0.045% 费率，"
                f"将 {fee_cost} {fee_currency} 估算为 ${estimated_usdt_fee:.6f} USDT。"
            )
            return estimated_usdt_fee
            
        else:
            logger.critical(
                f"！！！利润计算可能严重不准！！！\n"
                f"交易ID {data_obj.get('id')} 的手续费币种为未知的 '{fee_currency}'。\n"
                f"为防止数据污染，本次手续费将记为0。"
            )
            return 0.0

    if 'fee' in order_or_trade and isinstance(order_or_trade.get('fee'), dict):
        return process_fee_logic(order_or_trade['fee'], order_or_trade)
    
    if 'fees' in order_or_trade and isinstance(order_or_trade.get('fees'), list):
        fees_list = order_or_trade['fees']
        if not fees_list:
            return 0.0
        
        first_fee_currency = fees_list[0].get('currency')

        if first_fee_currency in ['USDT', 'BUSD']:
            return sum(f.get('cost', 0.0) for f in fees_list if isinstance(f, dict))
        else:
            return process_fee_logic(fees_list[0], order_or_trade)
            
    return 0.0
