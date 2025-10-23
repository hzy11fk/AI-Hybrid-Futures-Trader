# web_server.py (V16 - 前端加载优化版)
from aiohttp import web
import os
import logging
import asyncio
import pandas as pd
import numpy as np
import math
import json
import time
import collections # [新增] 导入 collections 用于高效读取日志

try:
    from helpers import setup_logging
    from config import settings, futures_settings
except ImportError:
    # --- Mock classes for standalone testing ---
    class MockSettings:
        FUTURES_INITIAL_PRINCIPAL = 1.0; TREND_SIGNAL_TIMEFRAME = '5m'; TREND_FILTER_TIMEFRAME = '15m'
        TREND_FILTER_MA_PERIOD = 30; ENTRY_RSI_PERIOD = 7; ENABLE_AI_MODE = True
    class MockFuturesSettings:
        PYRAMIDING_MAX_ADD_COUNT = 0; FUTURES_STATE_DIR = 'mock_data'; EXHAUSTION_ADX_PERIOD = 14
    settings = MockSettings(); futures_settings = MockFuturesSettings()
    def setup_logging(): logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')

def sanitize_data(data):
    if isinstance(data, dict): return {k: sanitize_data(v) for k, v in data.items()}
    if isinstance(data, list): return [sanitize_data(i) for i in data]
    if isinstance(data, (float, np.floating)):
        if math.isinf(data) or math.isnan(data): return None
        return float(data)
    if isinstance(data, np.integer): return int(data)
    if isinstance(data, np.bool_): return bool(data)
    if isinstance(data, pd.Timestamp): return data.isoformat()
    return data

async def _get_futures_trader_status(trader):
    # 此函数现在只从 trader 内存中读取数据，速度极快
    try:
        ai_status = {}
        if getattr(settings, 'ENABLE_AI_MODE', False) and hasattr(trader, 'ai_analyzer'):
            ai_trade_history = []
            if hasattr(trader, 'ai_performance_tracker') and hasattr(trader.ai_performance_tracker, 'trades'):
                 # 直接从 deque 获取列表，而不是调用一个不存在的 get_trade_history
                 ai_trade_history = list(trader.ai_performance_tracker.trades)

            ai_status = {
                "last_analysis": getattr(trader, 'last_ai_analysis_result', {}),
                "performance_score": trader.ai_performance_tracker.get_confidence_score() if hasattr(trader, 'ai_performance_tracker') else None,
                "paper_trade_position": getattr(trader, 'ai_paper_trade_position', {}),
                "trade_history": ai_trade_history # 新增字段
            }

        ui_cache = getattr(trader, 'ui_data_cache', {})
        ticker = ui_cache.get("ticker")
        ohlcv_5m_full = ui_cache.get("ohlcv_5m_full", [])
        
        if not ticker:
             return sanitize_data({"symbol": trader.symbol, "error": "正在等待交易机器人初始化数据..."})

        current_price = ticker.get('last')
        support_line_raw = ui_cache.get("support_line_raw")
        resistance_line_raw = ui_cache.get("resistance_line_raw")
        entry_zone_str = ui_cache.get("entry_zone")
        bollinger_bands_data = ui_cache.get("bollinger_bands")
        
        twelve_hours_ago_ms = (time.time() - 12 * 3600) * 1000
        price_history_for_frontend = [kline for kline in ohlcv_5m_full if kline[0] >= twelve_hours_ago_ms]

        position_status = trader.position.get_status()
        unrealized_pnl = 0.0
        if position_status.get('is_open') and current_price is not None:
            entry_price = position_status.get('entry_price', 0)
            size = position_status.get('size', 0)
            if entry_price > 0 and size > 0:
                if position_status['side'] == 'long': unrealized_pnl = (current_price - entry_price) * size
                else: unrealized_pnl = (entry_price - current_price) * size

        performance_stats = {}
        if hasattr(trader, 'profit_tracker'):
             performance_stats = {
                 "win_rate": trader.profit_tracker.win_rate, "payoff_ratio": trader.profit_tracker.payoff_ratio,
                 "max_drawdown": trader.profit_tracker.max_drawdown, "total_trades": len(trader.profit_tracker.trades_history)
             }

        full_status = {
            "symbol": trader.symbol, "current_price": current_price,
            "trend_result": trader.last_trend_analysis.get('final_trend', 'N/A'),
            "position": position_status, "unrealized_pnl": unrealized_pnl, 
            "price_history": price_history_for_frontend,
            "trend_analysis": trader.last_trend_analysis, "spike_analysis": trader.last_spike_analysis,
            "breakout_analysis": trader.last_breakout_analysis, "trendline_analysis": trader.last_trendline_analysis,
            "support_line_raw": support_line_raw,
            "resistance_line_raw": resistance_line_raw,
            "pyramiding_max_count": getattr(futures_settings, 'PYRAMIDING_MAX_ADD_COUNT', 0),
            "trend_exit_counter": getattr(trader, 'trend_exit_counter', 0),
            "performance": performance_stats, 
            "entry_zone": entry_zone_str, 
            "bollinger_bands": bollinger_bands_data,
            "momentum_analysis": getattr(trader, 'last_momentum_analysis', {}),
            "exhaustion_analysis": getattr(trader, 'last_exhaustion_analysis', {}),
            "ai_analysis": ai_status,
        }
        return sanitize_data(full_status)
    except Exception as e:
        logging.error(f"获取 {getattr(trader, 'symbol', 'Unknown')} 状态时出错: {e}", exc_info=True)
        return sanitize_data({"symbol": getattr(trader, 'symbol', 'Unknown'), "error": str(e)})

async def handle_all_statuses(request):
    """
    [修改] 此接口现在只返回快速的、内存中的数据。
    移除了缓慢的 fetch_balance 调用。
    """
    try:
        traders = request.app.get('traders')
        if not traders: return web.json_response({"error": "No traders running"}, status=404)
        
        # 1. 获取所有 trader 状态（快速，从内存读取）
        all_statuses = await asyncio.gather(*[_get_futures_trader_status(trader) for trader in traders.values()])
        
        # 2. 计算已实现利润（快速，从内存读取）
        total_realized_profit = sum(t.profit_tracker.get_total_profit() for t in traders.values() if hasattr(t, 'profit_tracker'))
        initial_principal = getattr(settings, 'FUTURES_INITIAL_PRINCIPAL', 1.0)
        profit_rate = (total_realized_profit / initial_principal) * 100 if initial_principal > 0 else 0.0
        
        # [移除] 移除了
        # total_equity = 0.0
        # balance_info = await list(traders.values())[0].exchange.fetch_balance(...)
        
        # 3. 立即返回，global_total_equity 由前端单独获取
        response_data = {
            "statuses": all_statuses, 
            "global_total_equity": None, # [修改] 设为 None，由新接口填充
            "total_realized_profit": total_realized_profit, 
            "profit_rate": profit_rate
        }
        return web.json_response(response_data, dumps=lambda x: json.dumps(sanitize_data(x)))
    except Exception as e:
        logging.error(f"处理 /api/status/all 请求失败: {e}", exc_info=True)
        return web.json_response({"error": f"Internal Server Error: {e}"}, status=500)

async def handle_global_equity(request):
    """
    [新增] 这是一个专门的慢速接口，只用于获取总权益。
    """
    traders = request.app.get('traders')
    if not traders: return web.json_response({"global_total_equity": 0.0, "error": "No traders"}, status=404)
    
    try:
        # 这是唯一的网络调用，被隔离在此
        balance_info = await list(traders.values())[0].exchange.fetch_balance({'type': 'swap'})
        total_equity = float(balance_info.get('total', {}).get('USDT', 0.0))
        return web.json_response({"global_total_equity": total_equity})
    except Exception as e:
        logging.error(f"获取合约账户总权益失败: {e}")
        return web.json_response({"global_total_equity": 0.0, "error": str(e)}, status=500)


async def handle_log_content(request):
    """
    [修改] 使用 collections.deque 高效读取日志文件末尾N行，避免读取整个文件。
    """
    log_path = os.path.join('logs', 'trading_system.log')
    if not os.path.exists(log_path): return web.Response(text="日志文件不存在")
    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            # 只在内存中保留最后 1000 行
            q = collections.deque(f, 1000)
        return web.Response(text=''.join(q))
    except Exception as e:
        return web.Response(text=f"读取日志错误: {e}")

async def handle_root(request):
    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <title>合约趋势策略监控</title>
        <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
        <style>
            .profit { color: #22c55e; } .loss { color: #ef4444; } .neutral { color: #9ca3af; }
            .long { color: #3b82f6; } .short { color: #f97316; }
            #initial-loader {
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background-color: #111827; display: flex; justify-content: center; align-items: center;
                z-index: 9999; flex-direction: column; gap: 1rem;
            }
            .spinner {
                border: 4px solid rgba(255, 255, 255, 0.3); border-radius: 50%;
                border-top: 4px solid #60a5fa; width: 50px; height: 50px;
                animation: spin 1s linear infinite;
            }
            @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        </style>
    </head>
    <body class="bg-gray-900 text-gray-200 font-sans">
        <div id="initial-loader">
            <div class="spinner"></div>
            <p class="text-gray-400">正在初始化监控面板...</p>
        </div>
        <div class="container mx-auto px-4 py-8">
            <h1 class="text-3xl md:text-4xl font-bold text-center text-white mb-6">合约趋势策略监控</h1>
            <div class="bg-gray-800 rounded-lg shadow-lg p-6 mb-10 text-center">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div><span class="text-gray-400 text-sm">合约账户总权益 (USDT)</span><p class="text-2xl md:text-3xl font-bold text-blue-400" id="global-equity">--</p></div>
                    <div><span class="text-gray-400 text-sm">已实现总盈亏 (USDT)</span><p class="text-2xl md:text-3xl font-bold" id="global-realized-profit">--</p></div>
                    <div><span class="text-gray-400 text-sm">总盈亏率</span><p class="text-2xl md:text-3xl font-bold" id="global-profit-rate">--</p></div>
                </div>
            </div>
            <div id="traders-grid" class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-8"></div>
            <div class="bg-gray-800 rounded-lg shadow-lg p-6 mt-10">
                <h2 class="text-2xl font-bold mb-4 text-white">系统实时日志</h2>
                 <div id="log-container" class="bg-black rounded h-96 overflow-y-auto p-4 font-mono text-sm"> <pre id="log-content" class="whitespace-pre-wrap break-words">正在加载日志...</pre> </div>
            </div>
        </div>
        <script>
            // --- [JS 修改] ---
            const chartInstances = {};
            
            // createTraderCardHTML 函数 (无变化)
            function createTraderCardHTML(status) {
                const symbolKey = status.symbol.replace(/[^a-zA-Z0-9]/g, '');
                return `
                <div class="bg-gray-800 rounded-lg shadow-lg p-6" id="card-${symbolKey}">
                    <h2 class="text-2xl font-bold mb-4 text-white flex items-center">${status.symbol} <span class="ml-2 text-xl trading-mode"></span></h2>
                    <div class="w-full h-72 mb-4 relative"> <canvas id="chart-${symbolKey}"></canvas></div>
                    <div class="space-y-4 text-sm">
                        <div class="grid grid-cols-2 gap-x-4 text-base">
                            <div><span class="text-gray-400">持仓方向:</span> <span class="font-semibold position-side">--</span></div>
                            <div><span class="text-gray-400">浮动盈亏:</span> <span class="font-semibold position-pnl">--</span></div>
                            <div><span class="text-gray-400">开仓均价:</span> <span class="font-mono position-entry">--</span></div>
                            <div><span class="text-gray-400">持仓数量:</span> <span class="font-mono position-size">--</span></div>
                            <div><span class="text-gray-400">加仓状态:</span> <span class="font-mono pyramiding-status">--</span></div>
                            <div><span class="text-gray-400">追踪止损:</span> <span class="font-mono text-red-400 position-sl">--</span></div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-gray-300">策略表现 (总交易: <span class="stat-total-trades">--</span>)</h3>
                            <div class="grid grid-cols-3 gap-x-2 text-center mt-2">
                                <div><span class="text-gray-400 text-xs">胜率</span><p class="font-mono text-base stat-win-rate">--</p></div>
                                <div><span class="text-gray-400 text-xs">盈亏比</span><p class="font-mono text-base stat-payoff-ratio">--</p></div>
                                <div><span class="text-gray-400 text-xs">最大回撤</span><p class="font-mono text-base stat-drawdown">--</p></div>
                            </div>
                        </div>
                        
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-gray-300">🤖 AI 决策分析</h3>
                             <div class="grid grid-cols-2 gap-x-4 text-xs mt-2">
                                 <div><span class="text-gray-400">AI 观点:</span> <span class="font-bold text-base ai-signal">--</span></div>
                                 <div><span class="text-gray-400">AI 置信度:</span> <span class="font-mono ai-confidence">--</span></div>
                                 <div class="col-span-2"><span class="text-gray-400">建议止损/盈:</span> <span class="font-mono ai-sl-tp">--</span></div>
                                 <div class="col-span-2 mt-1"><span class="text-gray-400">AI 分析师理由:</span> <p class="text-gray-300 ai-reason text-xs leading-relaxed">--</p></div>
                                 <div class="col-span-2 mt-2 pt-2 border-t border-gray-600 grid grid-cols-2 gap-x-4">
                                     <div>
                                         <span class="text-gray-400">历史绩效分:</span> 
                                         <span class="font-bold text-lg ai-performance-score">--</span>
                                     </div>
                                     <div>
                                         <span class="text-gray-400">AI模拟总盈亏:</span> 
                                         <span class="font-bold text-lg ai-total-pnl">--</span>
                                     </div>
                                 </div>
                                 <div class="col-span-2 mt-1">
                                    <span class="text-gray-400">当前模拟仓位:</span> <span class="font-mono ai-paper-trade">--</span>
                                 </div>
                                 <div class="col-span-2 mt-2 pt-2 border-t border-gray-600">
                                     <span class="text-gray-400">最近5笔模拟交易 (USDT):</span>
                                     <p class="font-mono text-xs ai-recent-trades text-gray-400">无记录</p>
                                 </div>
                                 </div>
                        </div>
                        
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-gray-300">入场动能确认: <span class="font-bold momentum-status">--</span></h3>
                             <div class="grid grid-cols-2 gap-x-4 text-xs mt-2">
                                 <div><span class="text-gray-400">RSI (动能):</span> <span class="font-mono momentum-rsi-value">--</span></div>
                                 <div><span class="text-gray-400">是否回升/落:</span> <span class="font-mono momentum-rebound-status">--</span></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-gray-300">趋势衰竭预警: <span class="font-bold exhaustion-status">--</span></h3>
                             <div class="grid grid-cols-2 gap-x-4 text-xs mt-2">
                                 <div><span class="text-gray-400">ADX (强度):</span> <span class="font-mono exhaustion-adx-value">--</span></div>
                                 <div><span class="text-gray-400">是否连续回落:</span> <span class="font-mono exhaustion-falling-status">--</span></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-gray-300">激增信号: <span class="font-bold spike-status">--</span></h3>
                             <div class="grid grid-cols-2 gap-x-4 text-xs mt-2">
                                 <div><span class="text-gray-400">K线实体/阈值:</span> <span class="font-mono spike-body">--</span></div>
                                 <div><span class="text-gray-400">成交量/阈值:</span> <span class="font-mono spike-volume">--</span></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-gray-300">突破信号: <span class="font-bold breakout-status">--</span></h3>
                            <div class="grid grid-cols-2 gap-x-4 text-xs mt-2">
                                <div class="col-span-2 mb-1"><span class="text-gray-400">波动率状态:</span> <span class="font-mono font-bold breakout-squeeze">--</span></div>
                                <div><span class="text-gray-400">RSI/阈值:</span> <span class="font-mono breakout-rsi">--</span></div>
                                <div><span class="text-gray-400">成交量/阈值:</span> <span class="font-mono breakout-volume">--</span></div>
                            </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                            <h3 class="font-semibold text-gray-300 text-lg">趋势分析 (当前价: <span class="font-mono current-price-val">--</span>)</h3>
                            <div class="grid grid-cols-2 gap-x-4 text-sm mt-2">
                                <div><span class="text-gray-400">5m信号:</span> <span class="font-semibold trend-signal">--</span></div>
                                <div><span class="text-gray-400">15m环境:</span> <span class="font-semibold trend-env">--</span></div>
                                <div><span class="text-gray-400">ADX:</span> <span class="font-mono trend-adx">--</span></div>
                                <div><span class="text-gray-400">确认状态:</span> <span class="font-semibold trend-confirmation">--</span></div>
                                <div class="col-span-2"><span class="text-gray-400">入场区:</span> <span class="font-mono trend-entry-zone">--</span></div>
                                <div class="col-span-2"><span class="text-gray-400">布林带:</span> <span class="font-mono trend-bbands">--</span></div>
                                <div class="col-span-2"><span class="text-gray-400">支撑/阻力:</span> <span class="font-mono trend-lines">--</span></div>
                                <div class="col-span-2 mt-2"><span class="text-gray-400">最终判断:</span> <span class="font-bold text-lg trend-result">--</span></div>
                            </div>
                        </div>
                    </div>
                </div>`;
            }

            // updateCard 函数 (无变化)
            function updateCard(card, status) {
                const updateText = (selector, text, defaultValue = '--') => {
                    const el = card.querySelector(selector);
                    if (el) el.textContent = (text !== null && text !== undefined && text !== '') ? String(text) : defaultValue;
                };
                const updateClass = (selector, baseClass, dynamicClass) => {
                    const el = card.querySelector(selector);
                    if(el) { el.classList.remove('profit', 'loss', 'neutral', 'long', 'short'); el.className = `${baseClass} ${dynamicClass}`; }
                };
                const pos = status.position || {};
                const analysis = status.trend_analysis || {};
                const details = analysis.details || {};
                const spike = status.spike_analysis || {};
                const perf = status.performance || {};
                const breakout = status.breakout_analysis || {};
                const trendline = status.trendline_analysis || {};
                const momentum = status.momentum_analysis || {};
                const exhaustion = status.exhaustion_analysis || {};
                const ai = status.ai_analysis || {};
                const ai_last = ai.last_analysis || {};
                const ai_paper = ai.paper_trade_position || {};

                if (ai && ai.trade_history && ai.trade_history.length > 0) {
                    const totalPnl = ai.trade_history.reduce((sum, trade) => sum + (trade.pnl || 0), 0);
                    updateText('.ai-total-pnl', `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}`);
                    updateClass('.ai-total-pnl', 'font-bold text-lg ai-total-pnl', totalPnl >= 0 ? 'profit' : 'loss');
                    const recentTrades = ai.trade_history.slice(-5).map(trade => {
                        const pnl = trade.pnl || 0;
                        return `<span class="${pnl >= 0 ? 'profit' : 'loss'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</span>`;
                    }).join(', ');
                    const recentTradesEl = card.querySelector('.ai-recent-trades');
                    if(recentTradesEl) recentTradesEl.innerHTML = recentTrades;
                } else {
                    updateText('.ai-total-pnl', '0.00');
                    updateClass('.ai-total-pnl', 'font-bold text-lg ai-total-pnl', 'neutral');
                    updateText('.ai-recent-trades', '无记录');
                }
                
                let signalText = '--';
                if(ai_last.signal) {
                    if (ai_last.signal === 'long') signalText = '看涨 📈';
                    else if (ai_last.signal === 'short') signalText = '看跌 📉';
                    else if (ai_last.signal === 'neutral') signalText = '中性 😑';
                }
                updateText('.ai-signal', signalText);
                updateClass('.ai-signal', 'font-bold text-base ai-signal', ai_last.signal === 'long' ? 'profit' : (ai_last.signal === 'short' ? 'loss' : 'neutral'));
                updateText('.ai-confidence', ai_last.confidence != null ? `${ai_last.confidence}%` : '--');
                const sl = ai_last.suggested_stop_loss, tp = ai_last.suggested_take_profit;
                updateText('.ai-sl-tp', (sl && tp) ? `SL: ${sl} / TP: ${tp}` : '--');
                updateText('.ai-reason', ai_last.reason, '等待AI分析...');
                updateText('.ai-performance-score', ai.performance_score != null ? `${ai.performance_score} / 100` : '--');
                updateClass('.ai-performance-score', 'font-bold text-lg ai-performance-score', ai.performance_score >= 60 ? 'profit' : (ai.performance_score < 40 ? 'loss' : 'neutral'));

                if (ai_paper && ai_paper.side) {
                    const pnl = (ai_paper.side === 'long') ? (status.current_price - ai_paper.entry_price) * ai_paper.size : (ai_paper.entry_price - status.current_price) * ai_paper.size;
                    const pnlText = `(${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} USDT)`;
                    updateText('.ai-paper-trade', `${ai_paper.side.toUpperCase()} @ ${ai_paper.entry_price.toFixed(4)} ${pnlText}`);
                    updateClass('.ai-paper-trade', 'font-mono ai-paper-trade', pnl >= 0 ? 'profit' : 'loss');
                } else {
                    updateText('.ai-paper-trade', '无');
                    updateClass('.ai-paper-trade', 'font-mono ai-paper-trade', 'neutral');
                }
                
                const tradingModeEl = card.querySelector('.trading-mode');
                if (tradingModeEl) {
                    let modeEmoji = '';
                    let modeTitle = '等待开仓';
                    if (pos.is_open && pos.entry_reason) {
                        switch (pos.entry_reason) {
                            case 'breakout_momentum_trade': modeEmoji = '⚡️'; modeTitle = '突破动能模式'; break;
                            case 'ranging_entry': modeEmoji = '⚖️'; modeTitle = '震荡均值回归'; break;
                            case 'pullback_entry': modeEmoji = '📈'; modeTitle = '趋势回调跟踪'; break;
                            case 'ai_entry': modeEmoji = '🤖'; modeTitle = 'AI决策模式'; break;
                            default: modeEmoji = '📈'; modeTitle = '趋势跟踪'; break;
                        }
                    }
                    tradingModeEl.textContent = modeEmoji;
                    tradingModeEl.title = modeTitle;
                }
                let sideText = pos.is_open ? pos.side.toUpperCase() : '无';
                if (pos.is_open && status.trend_exit_counter > 0) sideText += ` ⚠️(${status.trend_exit_counter})`;
                updateText('.position-side', sideText);
                updateClass('.position-side', 'font-semibold position-side', pos.side === 'long' ? 'long' : (pos.side === 'short' ? 'short' : 'neutral'));
                updateText('.position-pnl', pos.is_open ? status.unrealized_pnl.toFixed(2) : '--');
                updateClass('.position-pnl', 'font-semibold position-pnl', status.unrealized_pnl >= 0 ? 'profit' : 'loss');
                updateText('.position-entry', pos.is_open ? pos.entry_price.toFixed(4) : '--');
                updateText('.position-size', pos.is_open ? pos.size.toFixed(5) : '--');
                updateText('.pyramiding-status', pos.is_open ? `${pos.add_count} / ${status.pyramiding_max_count}` : '--');
                updateText('.position-sl', pos.is_open && pos.stop_loss > 0 ? pos.stop_loss.toFixed(4) : '--');
                updateText('.stat-total-trades', perf.total_trades);
                updateText('.stat-win-rate', perf.win_rate != null ? perf.win_rate.toFixed(2) + '%' : '--');
                updateText('.stat-payoff-ratio', perf.payoff_ratio != null ? perf.payoff_ratio.toFixed(2) : '--');
                updateText('.stat-drawdown', perf.max_drawdown != null ? perf.max_drawdown.toFixed(2) + '%' : '--');
                if (pos.is_open) {
                    updateText('.momentum-status', '持仓中不检测');
                    updateText('.spike-status', '持仓中不检测');
                    updateText('.breakout-status', '持仓中不检测');
                } else {
                    updateText('.momentum-status', momentum.status || '等待信号');
                    updateText('.spike-status', spike.status || '等待信号');
                    updateText('.breakout-status', breakout.status || '等待信号');
                }
                updateText('.momentum-rsi-value', momentum.rsi_value);
                updateText('.momentum-rebound-status', momentum.is_rebounding ? '✅' : (momentum.status !== 'Not Active' && momentum.status !== '持仓中不检测' ? '❌' : '--'));
                updateClass('.momentum-rebound-status', 'font-mono momentum-rebound-status', momentum.is_rebounding ? 'profit' : 'loss');
                updateText('.exhaustion-status', exhaustion.status);
                updateText('.exhaustion-adx-value', exhaustion.adx_value);
                updateText('.exhaustion-falling-status', exhaustion.is_falling ? '✅' : (exhaustion.status !== 'Not Active' ? '❌' : '--'));
                updateClass('.exhaustion-falling-status', 'font-mono exhaustion-falling-status', exhaustion.is_falling ? 'profit' : 'neutral');
                const bodyText = (spike.current_body != null && spike.body_threshold != null) ?
                    `${spike.current_body.toFixed(4)}/${spike.body_threshold.toFixed(4)}` : '--';
                updateText('.spike-body', bodyText);
                updateClass('.spike-body', 'font-mono spike-body', spike.current_body >= spike.body_threshold ? 'profit' : 'neutral');
                const volTextSpike = (spike.current_volume != null && spike.volume_threshold != null) ? `${spike.current_volume.toFixed(2)}/${spike.volume_threshold.toFixed(2)}` : '--';
                updateText('.spike-volume', volTextSpike);
                updateClass('.spike-volume', 'font-mono spike-volume', spike.current_volume >= spike.volume_threshold ? 'profit' : 'neutral');
                updateText('.breakout-squeeze', breakout.squeeze_status || 'N/A');
                updateClass('.breakout-squeeze', 'font-mono font-bold breakout-squeeze', breakout.squeeze_status === 'Squeezed' ? 'profit' : 'neutral');
                const rsiText = (breakout.rsi_value != null && breakout.rsi_threshold != null) ? `${breakout.rsi_value.toFixed(2)}/${breakout.rsi_threshold}` : '--';
                updateText('.breakout-rsi', rsiText);
                let isRsiMet = false;
                if (breakout.status && typeof breakout.status === 'string') {
                    if(breakout.status.includes('long')) { isRsiMet = breakout.rsi_value > breakout.rsi_threshold; }
                    else if(breakout.status.includes('short')) { isRsiMet = breakout.rsi_value < (100 - breakout.rsi_threshold); }
                }
                updateClass('.breakout-rsi', 'font-mono breakout-rsi', isRsiMet ? 'profit' : 'neutral');
                const volTextBreakout = (breakout.volume != null && breakout.volume_threshold != null) ? `${breakout.volume.toFixed(2)}/${breakout.volume_threshold.toFixed(2)}` : '--';
                updateText('.breakout-volume', volTextBreakout);
                updateClass('.breakout-volume', 'font-mono breakout-volume', breakout.volume >= breakout.volume_threshold ? 'profit' : 'neutral');
                updateText('.current-price-val', status.current_price ? status.current_price.toFixed(4) : '--');
                const signalTrend = analysis.signal_trend;
                updateText('.trend-signal', signalTrend === 'uptrend' ? '看涨' : (signalTrend === 'downtrend' ? '看跌' : (signalTrend ? '中性' : '--')));
                updateClass('.trend-signal', 'font-semibold trend-signal', signalTrend === 'uptrend' ? 'profit' : (signalTrend === 'downtrend' ? 'loss' : 'neutral'));
                const filterEnv = analysis.filter_env;
                updateText('.trend-env', filterEnv === 'bullish' ? '偏多' : (filterEnv === 'bearish' ? '偏空' : (filterEnv ? '盘整' : '--')));
                updateClass('.trend-env', 'font-semibold trend-env', filterEnv === 'bullish' ? 'profit' : (filterEnv === 'bearish' ? 'loss' : 'neutral'));
                const trendResult = status.trend_result;
                updateText('.trend-result', trendResult === 'uptrend' ? '上涨' : (trendResult === 'downtrend' ? '下跌' : '震荡'));
                updateClass('.trend-result', 'font-bold text-lg trend-result', trendResult === 'uptrend' ? 'profit' : (trendResult === 'downtrend' ? 'loss' : 'neutral'));
                updateText('.trend-adx', details.adx);
                updateText('.trend-confirmation', analysis.confirmation);
                updateText('.trend-entry-zone', status.entry_zone);
                const bbands = status.bollinger_bands;
                updateText('.trend-bbands', bbands && bbands.upper != null ? `${bbands.lower.toFixed(4)} / ${bbands.upper.toFixed(4)}` : '--');
                const support = trendline.support_price, resistance = trendline.resistance_price;
                let trendlineText = support ? `${support.toFixed(4)} / ` : '-- / ';
                trendlineText += resistance ? resistance.toFixed(4) : '--';
                updateText('.trend-lines', trendlineText);
            }

            // updateChartAndAnnotations 函数 (无变化)
            function updateChartAndAnnotations(status) {
                if (!status || !status.symbol || status.error) return;
                const chart = chartInstances[status.symbol];
                if (!chart) return;
                
                const chartData = (status.price_history || []).map(k => ({ x: k[0], y: k[4] }));
                chart.data.datasets[0].data = chartData;
                
                if (chartData.length > 0) {
                    const prices = chartData.map(d => d.y);
                    const minPrice = Math.min(...prices);
                    const maxPrice = Math.max(...prices);
                    const buffer = (maxPrice - minPrice) * 0.15;
                    chart.options.scales.y.min = minPrice - buffer;
                    chart.options.scales.y.max = maxPrice + buffer;
                }
                
                const annotations = {};
                const pos = status.position || {};
                if (pos.is_open) {
                    if (pos.entry_price > 0) annotations.entryLine = { type: 'line', yMin: pos.entry_price, yMax: pos.entry_price, borderColor: '#fbbf24', borderWidth: 1, borderDash: [5, 5], label: { content: '开仓价', enabled: true, position: 'start', backgroundColor: 'rgba(251, 191, 36, 0.5)' } };
                    if (pos.stop_loss > 0) annotations.stopLossLine = { type: 'line', yMin: pos.stop_loss, yMax: pos.stop_loss, borderColor: '#ef4444', borderWidth: 1, borderDash: [5, 5], label: { content: '止损价', enabled: true, position: 'start', backgroundColor: 'rgba(239, 68, 68, 0.5)' } };
                }
                if (chartData.length > 1) {
                    const chartStartTime = chartData[0].x, chartEndTime = chartData[chartData.length - 1].x;
                    if (status.support_line_raw) {
                        const { p1_ts, p1_price, slope } = status.support_line_raw;
                        annotations.supportTrendline = { type: 'line', xMin: chartStartTime, xMax: chartEndTime, yMin: p1_price + (chartStartTime - p1_ts) * slope, yMax: p1_price + (chartEndTime - p1_ts) * slope, borderColor: '#22c55e', borderWidth: 1, borderDash: [6, 6] };
                    }
                    if (status.resistance_line_raw) {
                        const { p1_ts, p1_price, slope } = status.resistance_line_raw;
                        annotations.resistanceTrendline = { type: 'line', xMin: chartStartTime, xMax: chartEndTime, yMin: p1_price + (chartStartTime - p1_ts) * slope, yMax: p1_price + (chartEndTime - p1_ts) * slope, borderColor: '#f97316', borderWidth: 1, borderDash: [6, 6] };
                    }
                }
                chart.options.plugins.annotation.annotations = annotations;
                chart.update('none');
            }

            // --- [核心修改] 更新数据获取和调度逻辑 ---

            // [新增] 专门更新主状态（卡片数据）的函数
            async function updateMainStatus() {
                try {
                    const statusResponse = await fetch('/api/status/all');
                    if (!statusResponse.ok) {
                        console.error('状态API错误:', statusResponse.status);
                        return;
                    }
                    const data = await statusResponse.json();
                    
                    // 1. 更新全局已实现盈亏 (这部分数据是快速的)
                    const profitEl = document.getElementById('global-realized-profit');
                    const rateEl = document.getElementById('global-profit-rate');
                    profitEl.textContent = data.total_realized_profit != null ? data.total_realized_profit.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '--';
                    rateEl.textContent = data.profit_rate != null ? data.profit_rate.toFixed(2) + '%' : '--';
                    profitEl.className = `text-2xl md:text-3xl font-bold ${data.total_realized_profit >= 0 ? 'profit' : 'loss'}`;
                    rateEl.className = `text-2xl md:text-3xl font-bold ${data.profit_rate >= 0 ? 'profit' : 'loss'}`;
                    
                    // 2. 更新所有交易卡片
                    const grid = document.getElementById('traders-grid');
                    if (data.statuses && Array.isArray(data.statuses)) {
                        data.statuses.forEach(status => {
                            if (!status || !status.symbol) return;
                            const symbolKey = status.symbol.replace(/[^a-zA-Z0-9]/g, '');
                            let card = document.getElementById(`card-${symbolKey}`);
                            if (!card) {
                                grid.insertAdjacentHTML('beforeend', createTraderCardHTML(status));
                                card = document.getElementById(`card-${symbolKey}`);
                            }
                            if (card) {
                                if(status.error) { card.innerHTML = `<h2 class="text-2xl font-bold text-white">${status.symbol}</h2><p class="text-red-400 mt-4">获取状态失败: ${status.error}</p>`; return; }
    
                                updateCard(card, status);
                                
                                // 3. 创建或更新图表
                                let chart = chartInstances[status.symbol];
                                if (!chart) {
                                    const ctx = document.getElementById(`chart-${symbolKey}`).getContext('2d');
                                    chart = new Chart(ctx, {
                                        type: 'line', data: { datasets: [{ label: '价格', data: [], borderColor: '#60a5fa', borderWidth: 2, pointRadius: 0 }] },
                                        options: { maintainAspectRatio: false, scales: { x: { type: 'time', time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } }, grid: { color: '#374151' } }, y: { position: 'right', grid: { color: '#374151' } } }, plugins: { legend: { display: false }, annotation: { annotations: {} } }, animation: false }
                                    });
                                    chartInstances[status.symbol] = chart;
                                }
                                updateChartAndAnnotations(status);
                            }
                        });
                    }
                } catch (error) {
                    console.error('更新主数据时发生严重错误:', error);
                }
            }
            
            // [新增] 专门更新慢速的总权益
            async function updateGlobalEquity() {
                try {
                    const equityResponse = await fetch('/api/global_equity');
                    if (!equityResponse.ok) { console.error('权益API错误:', equityResponse.status); return; }
                    const equityData = await equityResponse.json();
                    if (equityData && equityData.global_total_equity != null) {
                        document.getElementById('global-equity').textContent = equityData.global_total_equity.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                    }
                } catch (error) {
                    console.error('更新权益数据时出错:', error);
                }
            }

            // [新增] 专门更新慢速的日志
            async function updateLogs() {
                 try {
                    const logResponse = await fetch('/api/logs');
                    if (!logResponse.ok) { console.error('日志API错误:', logResponse.status); return; }
                    document.getElementById('log-content').textContent = await logResponse.text();
                    document.getElementById('log-container').scrollTop = document.getElementById('log-container').scrollHeight;
                 } catch (error) {
                     console.error('更新日志时出错:', error);
                 }
            }

            // [修改] 页面加载和轮询逻辑
            document.addEventListener('DOMContentLoaded', async () => {
                const loader = document.getElementById('initial-loader');
                
                // 1. 立即获取主状态（快速）
                await updateMainStatus();
                
                // 2. 隐藏加载器
                if (loader) {
                    loader.style.display = 'none';
                }
                
                // 3. 在页面显示后，再去获取慢速数据
                await Promise.all([
                    updateGlobalEquity(),
                    updateLogs()
                ]);
                
                // 4. 设置独立的轮询器
                setInterval(updateMainStatus, 15000); // 状态卡片（快速），15秒一次
                setInterval(updateGlobalEquity, 60000); // 总权益（慢速），60秒一次
                setInterval(updateLogs, 30000); // 日志（中速），30秒一次
            });
        </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def start_web_server(traders):
    app = web.Application()
    app['traders'] = traders
    app.router.add_get('/', handle_root)
    app.router.add_get('/api/status/all', handle_all_statuses)
    app.router.add_get('/api/global_equity', handle_global_equity) # [新增] 路由
    app.router.add_get('/api/logs', handle_log_content)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', 58182))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web监控服务已启动: http://0.0.0.0:{port}")
    return site
