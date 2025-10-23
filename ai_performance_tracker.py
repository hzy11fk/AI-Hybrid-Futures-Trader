# ai_performance_tracker.py (新建文件)

import logging
import json
import os
import pandas as pd
from collections import deque
from config import settings

class AIPerformanceTracker:
    def __init__(self, symbol: str, state_dir: str = 'data'):
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{symbol}]")
        self.symbol = symbol
        self.state_dir = state_dir
        self.state_file = os.path.join(self.state_dir, f'ai_performance_{self.symbol.replace("/", "_")}.json')
        self.trades = deque(maxlen=settings.AI_PERFORMANCE_LOOKBACK_TRADES)
        self.confidence_score = 50  # Start with a neutral score
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    # Use deque to automatically handle maxlen
                    self.trades = deque(state.get('trades', []), maxlen=settings.AI_PERFORMANCE_LOOKBACK_TRADES)
                    self.confidence_score = state.get('confidence_score', 50)
                self.logger.info(f"成功从 {self.state_file} 加载AI表现状态。")
            except Exception as e:
                self.logger.error(f"加载AI表现状态文件失败: {e}")
        else:
            self.logger.warning("未找到AI表现状态文件，将使用初始状态。")

    def _save_state(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump({
                    'trades': list(self.trades),
                    'confidence_score': self.confidence_score
                }, f, indent=4)
        except Exception as e:
            self.logger.error(f"保存AI表现状态文件失败: {e}")

    def record_trade(self, pnl: float):
        """记录一笔由AI决策的交易盈亏。"""
        if pnl is None: return
        self.trades.append({'pnl': pnl, 'is_win': pnl > 0})
        self.logger.info(f"记录一笔AI交易, PnL: {pnl:.2f} USDT。")
        self._calculate_score()
        self._save_state()
        
    def _calculate_score(self):
        """根据最近的交易历史计算置信度分数。"""
        num_trades = len(self.trades)
        if num_trades < 10: # 需要有足够的样本
            self.logger.info(f"AI交易样本 ({num_trades}) 过少，分数暂时不作大幅调整。")
            return

        df = pd.DataFrame(list(self.trades))
        
        # 1. 胜率 (权重 50%)
        win_rate = df['is_win'].sum() / num_trades
        win_rate_score = win_rate * 100

        # 2. 盈亏比 (权重 30%)
        wins = df[df['pnl'] > 0]['pnl']
        losses = df[df['pnl'] < 0]['pnl']
        avg_win = wins.mean() if not wins.empty else 0
        avg_loss = abs(losses.mean()) if not losses.empty else 0
        payoff_ratio = avg_win / avg_loss if avg_loss > 0 else 5.0 # 如果没有亏损，给一个很高的值
        # 将盈亏比标准化到 0-100
        payoff_score = min(payoff_ratio, 3.0) / 3.0 * 100 # 大于3的盈亏比都算满分
        
        # 3. 稳定性/夏普比率简化版 (权重 20%)
        pnl_std = df['pnl'].std()
        pnl_mean = df['pnl'].mean()
        stability_score = (pnl_mean / pnl_std) * 50 + 50 if pnl_std > 0 else 100 # 简化夏普，并映射到0-100
        stability_score = max(0, min(100, stability_score))

        # 综合加权分数
        final_score = (win_rate_score * 0.5) + (payoff_score * 0.3) + (stability_score * 0.2)
        
        # 平滑更新分数，避免剧烈波动
        self.confidence_score = self.confidence_score * 0.8 + final_score * 0.2
        self.confidence_score = int(max(0, min(100, self.confidence_score)))

        # --- [核心修改] 增加详细的日志输出 ---
        log_message = (
            f"--- AI 历史绩效评估报告 ---\n"
            f"  - 样本交易数: {num_trades} 笔\n"
            f"  - 胜率: {win_rate:.2%}\n"
            f"  - 盈亏比: {payoff_ratio:.2f}\n"
            f"  - 平均盈利: {avg_win:+.2f} USDT\n"
            f"  - 平均亏损: {avg_loss:-.2f} USDT\n"
            f"  - 最新绩效分数: {self.confidence_score} / 100"
        )
        self.logger.info(log_message)
        # --- 修改结束 ---
    def get_confidence_score(self) -> int:
        return self.confidence_score



# --- [新增] 方法，用于获取交易历史 ---
    def get_trade_history(self) -> list:
        """返回所有记录在案的交易历史。"""
        return list(self.trades)
