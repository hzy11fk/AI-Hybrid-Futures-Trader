# ai_analyzer.py (已重构为支持动态切换 AI 服务商)

import logging
import time
import json
import pandas as pd
import pandas_ta as ta
import requests
import asyncio

# 同时导入两个客户端及通用异常
from openai import OpenAI, AzureOpenAI, APIConnectionError, AuthenticationError, NotFoundError
from config import settings

class AIAnalyzer:
    def __init__(self, exchange, symbol: str):
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{symbol}]")
        self.exchange = exchange
        self.symbol = symbol
        
        self.client = None
        self.model_name = None
        self.provider_name = "N/A"

        # --- [核心修改] 根据配置文件动态初始化客户端 ---
        try:
            provider = getattr(settings, 'AI_PROVIDER', '').lower()

            if provider == 'azure':
                self.logger.info("检测到 AI_PROVIDER 为 'azure'，正在初始化 Azure OpenAI 客户端...")
                self.client = AzureOpenAI(
                    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                    api_key=settings.AZURE_OPENAI_KEY,
                    api_version=settings.AZURE_API_VERSION,
                )
                self.model_name = settings.AZURE_OPENAI_MODEL_NAME
                self.provider_name = "Azure OpenAI"
                self.logger.info(f"✅ Azure OpenAI 客户端初始化成功，使用部署模型: {self.model_name}")

            elif provider in ['openai', 'deepseek']:
                # 将 deepseek 视为 openai 的一种兼容实现
                effective_provider_name = "DeepSeek" if settings.OPENAI_API_BASE and "deepseek" in settings.OPENAI_API_BASE else "OpenAI"
                self.logger.info(f"检测到 AI_PROVIDER 为 '{provider}'，正在初始化标准 OpenAI 兼容客户端...")
                self.client = OpenAI(
                    api_key=settings.OPENAI_API_KEY,
                    base_url=settings.OPENAI_API_BASE,
                )
                self.model_name = settings.OPENAI_MODEL_NAME
                self.provider_name = effective_provider_name
                self.logger.info(f"✅ {self.provider_name} 客户端初始化成功，使用模型: {self.model_name}")
            
            else:
                self.logger.critical("❌ AI 初始化失败：未在配置文件中找到有效的 'AI_PROVIDER' 设置（应为 'azure' 或 'openai'）。")

        except AttributeError as e:
            self.logger.critical(f"❌ AI 初始化失败：配置文件中缺少 '{provider}' 服务商所需的凭据！错误: {e}")
            self.client = None
        # --- 修改结束 ---
        
        self.fear_greed_cache = {"timestamp": 0, "data": None}

    async def test_connection(self):
        """
        执行一个简单的API调用来测试与所选 AI 服务商的连接。
        """
        if not self.client:
            self.logger.critical("AI 客户端未初始化，连接测试跳过。")
            return False
            
        self.logger.info(f"正在测试与 {self.provider_name} 服务的连接...")
        try:
            # 通用的测试逻辑，适用于两个客户端
            self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": "say test"}],
                max_tokens=5, temperature=0.1
            )
            self.logger.info(f"✅ {self.provider_name} 连接测试成功！配置正确。")
            return True
        except AuthenticationError:
            self.logger.critical(f"❌ {self.provider_name} 连接测试失败: 认证错误！请检查对应的 API Key。")
            return False
        except NotFoundError:
            self.logger.critical(f"❌ {self.provider_name} 连接测试失败: 模型 '{self.model_name}' 未找到！请检查模型/部署名称。")
            return False
        except APIConnectionError:
            self.logger.critical(f"❌ {self.provider_name} 连接测试失败: 网络或终结点(Endpoint/Base URL)错误！")
            return False
        except Exception as e:
            self.logger.critical(f"❌ {self.provider_name} 连接测试发生未知错误: {e}", exc_info=True)
            return False

    def get_fear_and_greed_index(self):
        """获取并缓存恐惧贪婪指数，缓存1小时。"""
        # (此函数无需修改，保持您提供的版本)
        current_time = time.time()
        if current_time - self.fear_greed_cache["timestamp"] < 3600 and self.fear_greed_cache["data"]:
            return self.fear_greed_cache["data"]
        
        try:
            response = requests.get("https://api.alternative.me/fng/?limit=1")
            response.raise_for_status()
            data = response.json()['data'][0]
            self.fear_greed_cache = {"timestamp": current_time, "data": data}
            self.logger.info(f"成功获取恐惧贪婪指数: {data['value']} ({data['value_classification']})")
            return data
        except Exception as e:
            self.logger.error(f"获取恐惧贪婪指数失败: {e}")
            return None

    async def gather_market_data(self):
        """收集用于 AI 分析的各项技术指标和市场数据。"""
        # (此函数无需修改，保持您提供的版本)
        try:
            # 获取不同时间周期的 K 线数据
            ohlcv_15m, ohlcv_1h, ohlcv_4h = await asyncio.gather(
                self.exchange.fetch_ohlcv(self.symbol, '15m', limit=200),
                self.exchange.fetch_ohlcv(self.symbol, '1h', limit=200),
                self.exchange.fetch_ohlcv(self.symbol, '4h', limit=200)
            )

            if not ohlcv_15m or not ohlcv_1h or not ohlcv_4h:
                self.logger.warning("AI分析所需的一个或多个K线数据不足。")
                return None

            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # 使用 pandas_ta 计算指标
            df_15m.ta.rsi(length=14, append=True, col_names=('rsi_14',))
            df_15m.ta.macd(fast=12, slow=26, signal=9, append=True, col_names=('macd_12_26_9', 'macdh_12_26_9', 'macds_12_26_9'))
            df_15m.ta.bbands(length=20, std=2, append=True, col_names=('bbl_20_2', 'bbm_20_2', 'bbu_20_2', 'bbb_20_2', 'bbp_20_2'))
            df_15m.ta.ema(length=20, append=True, col_names=('ema_20',))
            df_15m.ta.ema(length=50, append=True, col_names=('ema_50',))
            df_15m.ta.adx(length=14, append=True, col_names=('adx_14', 'dmp_14', 'dmn_14', 'adx_aux_14'))
            df_15m.ta.atr(length=14, append=True, col_names=('atr_14',))

            # 提取最新指标
            latest_indicators = {
                "price": df_15m['close'].iloc[-1], "rsi_14": df_15m['rsi_14'].iloc[-1],
                "macd": df_15m['macd_12_26_9'].iloc[-1], "macdh": df_15m['macdh_12_26_9'].iloc[-1],
                "macds": df_15m['macds_12_26_9'].iloc[-1], "bbl_20_2": df_15m['bbl_20_2'].iloc[-1],
                "bbm_20_2": df_15m['bbm_20_2'].iloc[-1], "bbu_20_2": df_15m['bbu_20_2'].iloc[-1],
                "ema_20": df_15m['ema_20'].iloc[-1], "ema_50": df_15m['ema_50'].iloc[-1],
                "adx_14": df_15m['adx_14'].iloc[-1], "atr_14": df_15m['atr_14'].iloc[-1],
                "volume_avg_20": df_15m['volume'].rolling(20).mean().iloc[-1]
            }
            
            # 宏观趋势分析
            macro_trend = {
                "1h_ema_20_vs_50": "golden_cross" if df_1h['close'].ewm(span=20).mean().iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "dead_cross",
                "4h_ema_20_vs_50": "golden_cross" if df_4h['close'].ewm(span=20).mean().iloc[-1] > df_4h['close'].ewm(span=50).mean().iloc[-1] else "dead_cross",
            }
            
            # 市场情绪
            sentiment = self.get_fear_and_greed_index()

            return {
                "symbol": self.symbol, "current_price": latest_indicators.pop("price"),
                "indicators_15m": latest_indicators, "macro_trend": macro_trend, "sentiment": sentiment
            }
        except Exception as e:
            self.logger.error(f"收集市场数据时出错: {e}", exc_info=True)
            return None

    # 我将您之前的 `analyze_market_with_ai(self, market_data: dict)` 版本升级为支持性能反馈的版本
    async def analyze_market_with_ai(self, market_data: dict, performance_score: int = None):
        """构建 Prompt 并调用所选的 AI 服务商进行分析。"""
        if not self.client or not market_data:
            self.logger.warning("AI 客户端未初始化或市场数据为空，跳过分析。")
            return None

       
        system_prompt = """
        你是一位专业的加密货币市场分析师。你的任务是分析所提供的市场数据，并提供一个清晰、简洁、结构化的交易信号。
        你的分析必须严格基于所提供的数据。不要使用任何外部知识。
        你的回应必须是一个严格符合以下结构的有效JSON对象，其中 "reason" 字段必须使用中文进行解释：
        {
          "signal": "long",
          "reason": "这里是简洁的中文分析理由。",
          "confidence": 85,
          "suggested_entry_price": 68500.00,
          "suggested_stop_loss": 68000.50,
          "suggested_take_profit": 72000.00
        }

        "signal" 的可能值为: "long", "short", "neutral"。
        "confidence" 是一个 0 到 100 之间的整数，代表你的确定性。
        
        --- [!!] 新增要求 ---
        "suggested_entry_price" 是你建议的理想“限价单”入场价格。
        - 如果你认为应该立即入场（市价），请将此价格设置为非常接近当前价。
        - 如果你认为应该在回调时入场，请设置一个回调价格。
        - 如果信号是 "neutral"，此值可以为 null。
        --- [!!] 新增要求结束 ---

        "suggested_stop_loss" 和 "suggested_take_profit" 应基于波动率（ATR）和关键水平（如布林带或EMA）合理设定。如果信号是 "neutral"，这些值可以为 null。
        """


        feedback_instruction = ""
        if performance_score is not None:
            if performance_score < 40:
                feedback_instruction = (
                    "--- 重要指令：自我调整 ---\n"
                    f"你最近的历史绩效评分为 {performance_score} (0-100分)，表现不佳。\n"
                    "因此，在本次分析中，你需要更加谨慎和保守，优先考虑给出 'neutral'（中性）的判断，并降低 'confidence' 分数。\n"
                )
            elif performance_score > 75:
                feedback_instruction = (
                    "--- 参考信息：近期表现 ---\n"
                    f"你最近的历史绩效评分为 {performance_score} (0-100分)，表现优秀。请保持你当前的分析逻辑和风格。\n"
                )

        user_prompt = f"""
        Please analyze the following market data for {self.symbol} and provide a trading signal in the required JSON format.

        {feedback_instruction}

        Current Time: {pd.Timestamp.now(tz='UTC').isoformat()}
        Current Price: {market_data['current_price']}

        --- 15-Minute Chart Indicators ---
        {json.dumps(market_data['indicators_15m'], indent=2)}

        --- Macro Trend Context ---
        {json.dumps(market_data['macro_trend'], indent=2)}

        --- Market Sentiment ---
        {json.dumps(market_data['sentiment'], indent=2)}

        Based on a comprehensive analysis of all the above data, what is your trading signal?
        """

        try:
            self.logger.info(f"正在向 {self.provider_name} 发送分析请求...")
            # API 调用代码是通用的，无需修改
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            analysis_result = json.loads(response.choices[0].message.content)
            self.logger.info(f"成功接收到 AI 分析结果: {analysis_result}")
            return analysis_result

        except Exception as e:
            self.logger.error(f"调用 {self.provider_name} API 失败: {e}", exc_info=True)
            return None
