from aiohttp import web
import os
import logging
import asyncio
import pandas as pd
import numpy as np

try:
    from helpers import setup_logging
    from config import settings, futures_settings
except ImportError:
    class MockSettings: FUTURES_INITIAL_PRINCIPAL = 1.0
    class MockFuturesSettings: PYRAMIDING_MAX_ADD_COUNT = 0
    settings = MockSettings()
    futures_settings = MockFuturesSettings()
    def setup_logging():
        logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
        logging.info("使用了备用的基础日志配置。")

PULLBACK_ZONE_PERCENT = 0.4

async def _get_futures_trader_status(trader):
    """获取单个 FuturesTrendTrader 实例的状态字典 (已优化API调用)"""
    try:
        # --- [核心优化] 1. 集中获取所有需要的原材料 ---
        ohlcv_5m_limit = max(
            settings.TREND_LONG_MA_PERIOD, 
            settings.TREND_VOLUME_CONFIRM_PERIOD, 
            settings.TREND_RSI_CONFIRM_PERIOD,
            settings.BREAKOUT_BBANDS_PERIOD
        ) + 105
        
        ohlcv_15m_limit = max(
            settings.TREND_FILTER_MA_PERIOD, 
            settings.DYNAMIC_VOLUME_ATR_PERIOD_LONG
        ) + 55

        # 一次性并行获取所有基础数据
        base_data = await asyncio.gather(
            trader.exchange.fetch_ticker(trader.symbol),
            trader.exchange.fetch_ohlcv(trader.symbol, timeframe='5m', limit=ohlcv_5m_limit),
            trader.exchange.fetch_ohlcv(trader.symbol, timeframe='15m', limit=ohlcv_15m_limit),
            return_exceptions=True
        )
        ticker_result, ohlcv_5m, ohlcv_15m = base_data

        if isinstance(ticker_result, Exception): raise ticker_result
        if isinstance(ohlcv_5m, Exception): ohlcv_5m = []
        if isinstance(ohlcv_15m, Exception): ohlcv_15m = []

        current_price = ticker_result['last']
        price_history = ohlcv_5m[-300:] if len(ohlcv_5m) >= 300 else ohlcv_5m

        # --- [核心优化] 2. 将获取到的原材料分发给各个计算函数 ---
        # 并行执行所有计算，这些计算现在不再产生新的API调用
        computed_data = await asyncio.gather(
            trader._detect_trend(ohlcv_5m=ohlcv_5m, ohlcv_15m=ohlcv_15m),
            trader.get_entry_ema(ohlcv_data=ohlcv_5m),
            trader.get_atr_data(period=14, ohlcv_data=ohlcv_15m),
            trader.get_bollinger_bands_data(ohlcv_data=ohlcv_5m),
            return_exceptions=True
        )
        trend_result, ema_value, atr_value, bbands_data = computed_data

        if isinstance(trend_result, Exception): trend_result = 'ERROR'; trader.logger.error(f"Trend detection failed: {trend_result}")
        if isinstance(ema_value, Exception): ema_value = None; trader.logger.error(f"EMA calculation failed: {ema_value}")
        if isinstance(atr_value, Exception): atr_value = None; trader.logger.error(f"ATR calculation failed: {atr_value}")
        if isinstance(bbands_data, Exception): bbands_data = None; trader.logger.error(f"Bollinger Bands calculation failed: {bbands_data}")
        
        position_status = trader.position.get_status()
        unrealized_pnl = 0.0
        if position_status['is_open']:
            if position_status['side'] == 'long':
                unrealized_pnl = (current_price - position_status['entry_price']) * position_status['size']
            elif position_status['side'] == 'short':
                unrealized_pnl = (position_status['entry_price'] - current_price) * position_status['size']

        ema_upper_bound = None
        ema_lower_bound = None
        if ema_value is not None:
            zone_multiplier = PULLBACK_ZONE_PERCENT / 100.0
            ema_upper_bound = ema_value * (1 + zone_multiplier)
            ema_lower_bound = ema_value * (1 - zone_multiplier)

        performance_stats = {
            "win_rate": trader.profit_tracker.win_rate, "payoff_ratio": trader.profit_tracker.payoff_ratio,
            "max_drawdown": trader.profit_tracker.max_drawdown, "total_trades": len(trader.profit_tracker.trades_history)
        }

        return {
            "symbol": trader.symbol, "current_price": current_price, "trend_result": trend_result,
            "position": position_status, "unrealized_pnl": unrealized_pnl, "price_history": price_history,
            "trend_analysis": trader.last_trend_analysis,
            "ema_value": ema_value, "ema_upper_bound": ema_upper_bound, "ema_lower_bound": ema_lower_bound,
            "atr_value": atr_value, "pyramiding_max_count": futures_settings.PYRAMIDING_MAX_ADD_COUNT,
            "trend_exit_counter": trader.trend_exit_counter, "performance": performance_stats,
            "bbands": bbands_data,
            "spike_analysis": trader.last_spike_analysis
        }
    except Exception as e:
        logging.error(f"获取 {trader.symbol} 状态时出错: {e}", exc_info=True)
        return {"symbol": trader.symbol, "error": str(e)}
# ... handle_all_statuses, handle_log_content 函数保持不变 ...
async def handle_all_statuses(request):
    try:
        traders = request.app['traders']
        if not traders: return web.json_response({"error": "No traders running"}, status=404)
        tasks = [_get_futures_trader_status(trader) for trader in traders.values()]
        all_statuses = await asyncio.gather(*tasks)
        first_trader = list(traders.values())[0]
        total_realized_profit = sum(trader.profit_tracker.get_total_profit() for trader in traders.values())
        initial_principal = settings.FUTURES_INITIAL_PRINCIPAL
        profit_rate = (total_realized_profit / initial_principal) * 100 if initial_principal > 0 else 0
        total_equity = 0
        try:
            balance_info = await first_trader.exchange.fetch_balance({'type': 'swap'})
            total_equity = float(balance_info['total']['USDT'])
        except Exception as e: logging.error(f"获取合约账户总权益失败: {e}")
        response_data = {
            "statuses": all_statuses, "global_total_equity": total_equity,
            "total_realized_profit": total_realized_profit, "profit_rate": profit_rate,
        }
        return web.json_response(response_data)
    except Exception as e:
        logging.error(f"获取所有状态数据失败: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)

async def handle_log_content(request):
    log_path = os.path.join('logs', 'trading_system.log')
    if not os.path.exists(log_path): return web.Response(text="日志文件不存在")
    try:
        import aiofiles
        async with aiofiles.open(log_path, mode='rb') as f:
            await f.seek(0, os.SEEK_END)
            file_size = await f.tell()
            read_size = min(file_size, 1024 * 100)
            await f.seek(file_size - read_size)
            content_bytes = await f.read()
        content = content_bytes.decode('utf-8', errors='ignore')
        lines = content.strip().split('\n')
        lines.reverse()
        return web.Response(text='\n'.join(lines))
    except Exception as e:
        return web.Response(text=f"读取日志错误: {e}")

async def handle_log(request):
    """主页面渲染"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>合约趋势策略监控</title>
        <meta charset="utf-8">
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
        <style>
            .profit { color: #22c55e; } .loss { color: #ef4444; } .neutral { color: #6b7280; }
            .long { color: #3b82f6; } .short { color: #f97316; } .bullish { color: #10b981; } .bearish { color: #ef4444; }
        </style>
    </head>
    <body class="bg-gray-900 text-gray-200 font-sans">
        <div class="container mx-auto px-4 py-8">
            <h1 class="text-4xl font-bold text-center text-white mb-6">合约趋势策略监控</h1>
            <div class="bg-gray-800 rounded-lg shadow-lg p-6 mb-10 text-center">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div><span class="text-gray-400 text-sm">合约账户总权益 (USDT)</span><p class="text-3xl font-bold text-blue-400" id="global-equity">--</p></div>
                    <div><span class="text-gray-400 text-sm">已实现总盈亏 (USDT)</span><p class="text-3xl font-bold" id="global-realized-profit">--</p></div>
                    <div><span class="text-gray-400 text-sm">总盈亏率</span><p class="text-3xl font-bold" id="global-profit-rate">--</p></div>
                </div>
            </div>
            <div id="traders-grid" class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-8"></div>
            <div class="bg-gray-800 rounded-lg shadow-lg p-6 mt-10">
                 <h2 class="text-2xl font-bold mb-4 text-white">系统实时日志</h2>
                 <div class="bg-black rounded h-96 overflow-y-auto p-4 font-mono text-sm">
                    <pre id="log-content">正在加载日志...</pre>
                 </div>
            </div>
        </div>
        <script>
            const chartInstances = {};
            function createTraderCardHTML(status) {
                return `
                <div class="bg-gray-800 rounded-lg shadow-lg p-6" id="card-${status.symbol.replace(/[^a-zA-Z0-9]/g, '')}">
                    <h2 class="text-2xl font-bold mb-4 text-white">${status.symbol}</h2>
                    <div class="w-full h-72 mb-4"><canvas id="chart-${status.symbol.replace(/[^a-zA-Z0-9]/g, '')}"></canvas></div>
                    <div class="space-y-3 text-sm">
                        <div class="grid grid-cols-2 gap-x-4">
                            <div><span class="text-gray-400">持仓方向</span><p class="text-lg font-semibold position-side">--</p></div>
                            <div><span class="text-gray-400">浮动盈亏</span><p class="text-lg font-semibold position-pnl">--</p></div>
                            <div><span class="text-gray-400">开仓均价</span><p class="text-base font-semibold position-entry">--</p></div>
                            <div><span class="text-gray-400">持仓数量</span><p class="text-base font-semibold position-size">--</p></div>
                        </div>
                        <div class="grid grid-cols-2 gap-x-4 pt-3 border-t border-gray-700">
                            <div><span class="text-gray-400">加仓状态</span><p class="text-base font-semibold pyramiding-status">--</p></div>
                            <div><span class="text-gray-400">追踪止损</span><p class="text-base font-semibold text-red-400 position-sl">--</p></div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-base mb-2 text-gray-300">策略表现 (总交易: <span class="stat-total-trades">--</span>)</h3>
                             <div class="grid grid-cols-3 gap-x-2 text-center">
                                <div><span class="text-gray-400 text-xs">胜率</span><p class="font-mono text-lg stat-win-rate">--</p></div>
                                <div><span class="text-gray-400 text-xs">盈亏比</span><p class="font-mono text-lg stat-payoff-ratio">--</p></div>
                                <div><span class="text-gray-400 text-xs">最大回撤</span><p class="font-mono text-lg stat-drawdown">--</p></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-base mb-2 text-gray-300">激增信号监控 (实时)</h3>
                             <div class="grid grid-cols-2 gap-x-4">
                                <div><span class="text-gray-400">K线实体/阈值</span><p class="font-mono text-base spike-body">--</p></div>
                                <div><span class="text-gray-400">实时成交量/阈值</span><p class="font-mono text-base spike-volume">--</p></div>
                                <div class="col-span-2"><span class="text-gray-400">状态</span><p class="font-bold text-base spike-status">--</p></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-base mb-2 text-gray-300">回调信号监控 (EMA)</h3>
                             <div class="grid grid-cols-2 gap-x-4">
                                <div><span class="text-gray-400">当前价格</span><p class="font-mono text-lg current-price-val">--</p></div>
                                <div><span class="text-gray-400">ATR (15m)</span><p class="font-mono text-lg atr-value">--</p></div>
                                <div class="col-span-2"><span class="text-gray-400">EMA 入场机会区</span><p class="font-mono text-lg ema-range-val">--</p></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                             <h3 class="font-semibold text-base mb-2 text-gray-300">突破信号监控 (布林带)</h3>
                             <div class="grid grid-cols-3 gap-x-2 text-center">
                                <div><span class="text-gray-400 text-xs">上轨</span><p class="font-mono text-base bbands-upper">--</p></div>
                                <div><span class="text-gray-400 text-xs">中轨</span><p class="font-mono text-base bbands-middle">--</p></div>
                                <div><span class="text-gray-400 text-xs">下轨</span><p class="font-mono text-base bbands-lower">--</p></div>
                             </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                            <h3 class="font-semibold text-base mb-2 text-gray-300">趋势决策分析</h3>
                            <div class="grid grid-cols-2 gap-x-4">
                                <div><span class="text-gray-400">5m 信号</span><p class="font-semibold text-base trend-signal">--</p></div>
                                <div><span class="text-gray-400">15m 环境</span><p class="font-semibold text-base trend-env">--</p></div>
                                <div><span class="text-gray-400">ADX (15m)</span><p class="font-mono text-base trend-adx">--</p></div>
                                <div><span class="text-gray-400">最终趋势</span><p class="font-bold text-base trend-result">--</p></div>
                            </div>
                        </div>
                        <div class="pt-3 border-t border-gray-700">
                            <h3 class="font-semibold text-base mb-2 text-gray-300">趋势确认信号 (5m)</h3>
                            <div class="grid grid-cols-2 gap-x-4">
                                <div><span class="text-gray-400">上根K线成交量</span><p class="font-mono text-base trend-volume-current">--</p></div>
                                <div><span class="text-gray-400">成交量均线</span><p class="font-mono text-base trend-volume-vma">--</p></div>
                                <div><span class="text-gray-400">RSI 动量</span><p class="font-mono text-base trend-rsi">--</p></div>
                                <div><span class="text-gray-400">确认状态</span><p class="font-bold text-base trend-confirmation">--</p></div>
                            </div>
                        </div>
                    </div>
                </div>`;
            }
            async function updateData() {
                try {
                    const [statusResponse, logResponse] = await Promise.all([ fetch('/api/status/all'), fetch('/api/logs') ]);
                    if (!statusResponse.ok) throw new Error('Status API failed');
                    const data = await statusResponse.json();
                    if (logResponse.ok) { document.getElementById('log-content').textContent = await logResponse.text(); }
                    
                    document.getElementById('global-equity').textContent = data.global_total_equity ? data.global_total_equity.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '--';
                    const profitEl = document.getElementById('global-realized-profit');
                    const rateEl = document.getElementById('global-profit-rate');
                    profitEl.textContent = data.total_realized_profit != null ? data.total_realized_profit.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '--';
                    rateEl.textContent = data.profit_rate != null ? data.profit_rate.toFixed(2) + '%' : '--';
                    profitEl.className = `text-3xl font-bold ${data.total_realized_profit >= 0 ? 'profit' : 'loss'}`;
                    rateEl.className = `text-3xl font-bold ${data.profit_rate >= 0 ? 'profit' : 'loss'}`;
                    
                    const grid = document.getElementById('traders-grid');
                    for (const status of data.statuses) {
                        if (status.error) { 
                            console.error(`后端状态错误 for ${status.symbol}: ${status.error}`);
                             if (!document.getElementById(`card-${status.symbol.replace(/[^a-zA-Z0-9]/g, '')}`)) {
                                grid.insertAdjacentHTML('beforeend', createTraderCardHTML(status));
                             }
                            continue; 
                        }
                        const symbolKey = status.symbol.replace(/[^a-zA-Z0-9]/g, '');
                        if (!document.getElementById(`card-${symbolKey}`)) {
                            grid.insertAdjacentHTML('beforeend', createTraderCardHTML(status));
                        }
                        const card = document.getElementById(`card-${symbolKey}`);
                        if (!card) continue;
                        
                        // --- [核心修改] 更新激增信号板块 ---
                        if (status.spike_analysis) {
                            const spike = status.spike_analysis;
                            const bodyEl = card.querySelector('.spike-body');
                            const volumeEl = card.querySelector('.spike-volume');
                            const statusEl = card.querySelector('.spike-status');

                            bodyEl.textContent = (spike.current_body != null && spike.body_threshold != null) ? `${spike.current_body.toFixed(4)} / ${spike.body_threshold.toFixed(4)}` : '--';
                            if(spike.current_body != null && spike.body_threshold != null) {
                                bodyEl.className = `font-mono text-base spike-body ${spike.current_body >= spike.body_threshold ? 'profit' : 'neutral'}`;
                            }

                            volumeEl.textContent = (spike.current_volume != null && spike.volume_threshold != null) ? `${spike.current_volume.toFixed(2)} / ${spike.volume_threshold.toFixed(2)}` : '--';
                             if(spike.current_volume != null && spike.volume_threshold != null) {
                                volumeEl.className = `font-mono text-base spike-volume ${spike.current_volume >= spike.volume_threshold ? 'profit' : 'neutral'}`;
                            }
                            
                            statusEl.textContent = spike.status || '--';
                            let statusClass = 'neutral';
                            if (spike.status && spike.status.includes('Triggered')) statusClass = 'profit';
                            else if (spike.status && (spike.status.includes('low') || spike.status.includes('small'))) statusClass = 'loss';
                            statusEl.className = `font-bold text-base spike-status ${statusClass}`;
                        }

                        if (status.performance) {
                            const perf = status.performance;
                            card.querySelector('.stat-total-trades').textContent = perf.total_trades != null ? perf.total_trades : '--';
                            card.querySelector('.stat-win-rate').textContent = perf.win_rate != null ? perf.win_rate.toFixed(2) + '%' : '--';
                            card.querySelector('.stat-payoff-ratio').textContent = perf.payoff_ratio != null ? perf.payoff_ratio.toFixed(2) : '--';
                            card.querySelector('.stat-drawdown').textContent = perf.max_drawdown != null ? perf.max_drawdown.toFixed(2) + '%' : '--';
                        }
                        
                        if (status.bbands) {
                            card.querySelector('.bbands-upper').textContent = status.bbands.upper ? status.bbands.upper.toFixed(4) : '--';
                            card.querySelector('.bbands-middle').textContent = status.bbands.middle ? status.bbands.middle.toFixed(4) : '--';
                            card.querySelector('.bbands-lower').textContent = status.bbands.lower ? status.bbands.lower.toFixed(4) : '--';
                        }

                        // ... (其他数据更新逻辑保持不变) ...
                        card.querySelector('.current-price-val').textContent = status.current_price ? status.current_price.toFixed(4) : '--';
                        card.querySelector('.atr-value').textContent = status.atr_value ? status.atr_value.toFixed(4) : '--';
                        const emaRangeEl = card.querySelector('.ema-range-val');
                        if(status.ema_lower_bound && status.ema_upper_bound) {
                            emaRangeEl.textContent = `${status.ema_lower_bound.toFixed(4)} - ${status.ema_upper_bound.toFixed(4)}`;
                        } else {
                             emaRangeEl.textContent = '--';
                        }
                        const pos = status.position;
                        const positionSideEl = card.querySelector('.position-side');
                        let sideText = pos.is_open ? pos.side.toUpperCase() : '无';
                        if (pos.is_open && status.trend_exit_counter > 0) { sideText += ` ⚠️(${status.trend_exit_counter})`; }
                        positionSideEl.textContent = sideText;
                        positionSideEl.className = `text-lg font-semibold position-side ${pos.side === 'long' ? 'long' : (pos.side === 'short' ? 'short' : 'neutral')}`;
                        card.querySelector('.position-pnl').textContent = pos.is_open ? status.unrealized_pnl.toFixed(2) : '--';
                        card.querySelector('.position-pnl').className = `text-lg font-semibold position-pnl ${status.unrealized_pnl >= 0 ? 'profit' : 'loss'}`;
                        card.querySelector('.position-entry').textContent = pos.is_open ? pos.entry_price.toFixed(4) : '--';
                        card.querySelector('.position-size').textContent = pos.is_open ? pos.size.toFixed(5) : '--';
                        card.querySelector('.pyramiding-status').textContent = pos.is_open ? `${pos.add_count} / ${status.pyramiding_max_count}` : '--';
                        card.querySelector('.position-sl').textContent = pos.is_open && pos.stop_loss > 0 ? pos.stop_loss.toFixed(4) : '--';
                        
                        const analysis = status.trend_analysis;
                        if (analysis) {
                            const signalEl = card.querySelector('.trend-signal');
                            signalEl.textContent = analysis.signal_trend === 'uptrend' ? '看涨' : (analysis.signal_trend === 'downtrend' ? '看跌' : '中性');
                            signalEl.className = `font-semibold text-base trend-signal ${analysis.signal_trend === 'uptrend' ? 'profit' : (analysis.signal_trend === 'downtrend' ? 'loss' : 'neutral')}`;
                            const envEl = card.querySelector('.trend-env');
                            envEl.textContent = analysis.filter_env === 'bullish' ? '偏多' : '偏空';
                            envEl.className = `font-semibold text-base trend-env ${analysis.filter_env === 'bullish' ? 'bullish' : 'bearish'}`;
                            card.querySelector('.trend-adx').textContent = analysis.adx_value != null ? analysis.adx_value.toFixed(2) : '--';
                            const volCurrentEl = card.querySelector('.trend-volume-current');
                            const volVmaEl = card.querySelector('.trend-volume-vma');
                            volCurrentEl.textContent = analysis.current_volume != null ? analysis.current_volume.toFixed(2) : '--';
                            volVmaEl.textContent = analysis.vma != null ? analysis.vma.toFixed(2) : '--';
                            if (analysis.current_volume != null && analysis.vma != null) {
                                volCurrentEl.className = `font-mono text-base trend-volume-current ${analysis.current_volume >= analysis.vma * 1.5 ? 'profit' : 'neutral'}`;
                            }
                            const rsiEl = card.querySelector('.trend-rsi');
                            rsiEl.textContent = analysis.rsi != null ? analysis.rsi.toFixed(2) : '--';
                            if (analysis.rsi != null) {
                                let rsiClass = 'neutral';
                                if (analysis.rsi >= 55) rsiClass = 'bullish';
                                else if (analysis.rsi <= 45) rsiClass = 'bearish';
                                rsiEl.className = `font-mono text-base trend-rsi ${rsiClass}`;
                            }
                            const confEl = card.querySelector('.trend-confirmation');
                            confEl.textContent = analysis.confirmation || '--';
                            let confClass = 'neutral';
                            if (analysis.confirmation === 'Passed') confClass = 'profit';
                            else if (analysis.confirmation && analysis.confirmation.includes('Failed')) confClass = 'loss';
                            confEl.className = `font-bold text-base trend-confirmation ${confClass}`;
                        }

                        const resultEl = card.querySelector('.trend-result');
                        resultEl.textContent = status.trend_result === 'uptrend' ? '上涨' : (status.trend_result === 'downtrend' ? '下跌' : '震荡');
                        resultEl.className = `font-bold text-base trend-result ${status.trend_result === 'uptrend' ? 'profit' : (status.trend_result === 'downtrend' ? 'loss' : 'neutral')}`;
                        
                        const chartData = (status.price_history || []).map(k => ({ x: k[0], y: k[4] }));
                        let chart = chartInstances[status.symbol];
                        if (!chart) {
                            const ctx = document.getElementById(`chart-${symbolKey}`).getContext('2d');
                            chart = new Chart(ctx, {
                                type: 'line',
                                data: { 
                                    datasets: [
                                        { label: '价格', data: [], borderColor: '#60a5fa', borderWidth: 2, pointRadius: 0, tension: 0.1 },
                                        { label: '开仓价', data: [], borderColor: '#fbbf24', borderWidth: 1.5, borderDash: [5, 5], type: 'line' },
                                        { label: '止损价', data: [], borderColor: '#f87171', borderWidth: 1.5, borderDash: [2, 2], type: 'line' },
                                        { label: 'EMA 上轨', data: [], borderColor: 'rgba(110, 231, 183, 0.4)', borderWidth: 1, pointRadius: 0, borderDash: [3, 3] },
                                        { label: 'EMA 下轨', data: [], borderColor: 'rgba(110, 231, 183, 0.4)', borderWidth: 1, pointRadius: 0, borderDash: [3, 3], 
                                          fill: '-1', backgroundColor: 'rgba(16, 185, 129, 0.1)' },
                                        { label: 'BB 上轨', data: [], borderColor: 'rgba(168, 85, 247, 0.5)', borderWidth: 1, pointRadius: 0, borderDash: [4, 4] },
                                        { label: 'BB 中轨', data: [], borderColor: 'rgba(168, 85, 247, 0.5)', borderWidth: 1, pointRadius: 0, borderDash: [2, 2] },
                                        { label: 'BB 下轨', data: [], borderColor: 'rgba(168, 85, 247, 0.5)', borderWidth: 1, pointRadius: 0, borderDash: [4, 4] }
                                    ]
                                },
                                options: {
                                    maintainAspectRatio: false,
                                    scales: { 
                                        x: { type: 'time', time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } }, grid: { color: '#374151' }, ticks: { color: '#9ca3af', maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }}, 
                                        y: { position: 'right', grid: { color: '#374151' }, ticks: { color: '#9ca3af' }}
                                    },
                                    plugins: { legend: { display: false } },
                                    animation: { duration: 0 }
                                }
                            });
                            chartInstances[status.symbol] = chart;
                        }
                        
                        chart.data.datasets[0].data = chartData;
                        const firstX = chartData.length > 0 ? chartData[0].x : null;
                        const lastX = chartData.length > 0 ? chartData[chartData.length - 1].x : null;

                        if (pos.is_open && firstX && lastX) {
                            chart.data.datasets[1].data = [{x: firstX, y: pos.entry_price}, {x: lastX, y: pos.entry_price}];
                            chart.data.datasets[2].data = [{x: firstX, y: pos.stop_loss}, {x: lastX, y: pos.stop_loss}];
                        } else {
                            chart.data.datasets[1].data = []; chart.data.datasets[2].data = [];
                        }
                        
                        if (!pos.is_open && firstX && lastX && status.ema_upper_bound && status.ema_lower_bound) {
                            chart.data.datasets[3].data = [{x: firstX, y: status.ema_upper_bound}, {x: lastX, y: status.ema_upper_bound}];
                            chart.data.datasets[4].data = [{x: firstX, y: status.ema_lower_bound}, {x: lastX, y: status.ema_lower_bound}];
                        } else {
                            chart.data.datasets[3].data = []; chart.data.datasets[4].data = [];
                        }
                        
                        if (!pos.is_open && firstX && lastX && status.bbands) {
                            chart.data.datasets[5].data = [{x: firstX, y: status.bbands.upper}, {x: lastX, y: status.bbands.upper}];
                            chart.data.datasets[6].data = [{x: firstX, y: status.bbands.middle}, {x: lastX, y: status.bbands.middle}];
                            chart.data.datasets[7].data = [{x: firstX, y: status.bbands.lower}, {x: lastX, y: status.bbands.lower}];
                        } else {
                            chart.data.datasets[5].data = [];
                            chart.data.datasets[6].data = [];
                            chart.data.datasets[7].data = [];
                        }
                        
                        chart.update('none');
                    }
                } catch (error) { console.error('更新状态时发生错误:', error); }
            }
            document.addEventListener('DOMContentLoaded', () => { updateData(); setInterval(updateData, 5000); });
        </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def start_web_server(traders):
    app = web.Application()
    app['traders'] = traders
    app.router.add_get('/', handle_log)
    app.router.add_get('/api/status/all', handle_all_statuses)
    app.router.add_get('/api/logs', handle_log_content)
    runner = web.AppRunner(app)
    await runner.setup()
    new_port = 58182
    site = web.TCPSite(runner, '0.0.0.0', new_port)
    await site.start()
    logging.info(f"Web监控服务已启动: http://0.0.0.0:{new_port}")
