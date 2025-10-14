# 文件: helpers.py (最终完整版)

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
        
        # 清理所有已存在的 handlers，防止日志重复打印
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # 确保日志目录存在
        if not os.path.exists(LogConfig.LOG_DIR):
            os.makedirs(LogConfig.LOG_DIR)
        
        # 文件处理器，按天轮换日志
        file_handler = TimedRotatingFileHandler(
            os.path.join(LogConfig.LOG_DIR, LogConfig.LOG_FILENAME),
            when='midnight', interval=1, backupCount=LogConfig.BACKUP_DAYS, encoding='utf-8', delay=True
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(name)-20s] %(levelname)-8s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
        ))
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s: %(message)s'))
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        # 过滤掉 aiohttp 库的访问日志，保持日志清爽
        logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

def setup_logging():
    """
    顶层函数，用于调用LogConfig类中的日志设置方法。
    这个函数是程序的日志入口点。
    """
    LogConfig.setup_logger()
    logging.info("==================================================")
    logging.info("日志系统已初始化")
    logging.info("==================================================")


def send_bark_notification(content: str, title: str = "合约策略通知"):
    """
    [修正版] 发送通知到 Bark App，使用查询参数以避免特殊字符问题。
    """
    if not settings.BARK_URL_KEY:
        logging.warning("未配置 BARK_URL_KEY，无法发送Bark通知。")
        return

    try:
        # 基础URL是您的Key，不再拼接任何可变内容
        base_url = settings.BARK_URL_KEY
        
        # 将 title 和 content(body) 放入 params 字典
        # requests库会自动处理所有特殊字符的URL编码
        params = {
            'title': title,
            'body': content,
  #          "icon": "https://raw.githubusercontent.com/finfinpro/server/main/Logo.png",
            "copy": content
        }
        
        logging.info(f"正在发送Bark通知: {title}")
        # 使用 GET 请求，并将所有可变内容作为参数传递
        response = requests.get(base_url, params=params, timeout=5)
        
        if response.status_code == 200:
            logging.info("Bark通知发送成功。")
        else:
            # 这里的日志能更准确地反映出Bark服务器返回的真实错误
            logging.error(f"Bark通知发送失败: 状态码={response.status_code}, 响应={response.text}")
            
    except Exception as e:
        logging.error(f"发送Bark通知时发生异常: {e}", exc_info=True)
