import logging
import os
import json
import time
import math

from config import settings

class ProfitTracker:
    """
    V4 - 全功能业绩记录器，已集成资金费用同步和性能指标计算。
    """
    def __init__(self, state_dir: str, symbol: str, initial_principal: float):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.symbol}]")
        self.initial_principal = initial_principal
        
        self.total_profit = 0.0
        self.trades_history = []
        self.equity_history = []
        self.last_funding_fee_timestamp = 0

        self.is_new = True
        safe_symbol = symbol.replace('/', '_').replace(':', '_')
        self.state_file = os.path.join(state_dir, f'futures_profit_tracker_{safe_symbol}.json')
        
        os.makedirs(state_dir, exist_ok=True)
        self.load_state()

    @property
    def win_rate(self) -> float | None:
        """计算胜率"""
        total_trades = len(self.trades_history)
        if total_trades == 0:
            return None
        winning_trades = len([pnl for pnl in self.trades_history if pnl > 0])
        return (winning_trades / total_trades) * 100

    @property
    def payoff_ratio(self) -> float | None:
        """计算盈亏比"""
        wins = [pnl for pnl in self.trades_history if pnl > 0]
        losses = [pnl for pnl in self.trades_history if pnl < 0]

        if not wins or not losses:
            return None

        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))

        if avg_loss == 0:
            return float('inf')
        
        return avg_win / avg_loss

    @property
    def max_drawdown(self) -> float | None:
        """计算最大回撤率"""
        if len(self.equity_history) < 2:
            return None

        peak = -float('inf')
        max_dd = 0.0
        
        for record in self.equity_history:
            equity = record['equity']
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak if peak > 0 else 0
            if drawdown > max_dd:
                max_dd = drawdown
        
        return max_dd * 100

    def get_performance_score(self) -> float | None:
        """
        计算综合表现得分 (0.0 - 1.0)。
        如果交易次数不足，返回 None。
        """
        if len(self.trades_history) < settings.MIN_TRADES_FOR_EVALUATION:
            return None

        win_rate_val = self.win_rate
        payoff_ratio_val = self.payoff_ratio
        max_drawdown_val = self.max_drawdown

        if win_rate_val is None or payoff_ratio_val is None or max_drawdown_val is None:
            return None

        score_wr = win_rate_val / 100.0
        score_pr = 1 / (1 + math.exp(-2 * (payoff_ratio_val - 1.5)))
        score_dd = 1.0 - (max_drawdown_val / 100.0)

        final_score = (score_wr * settings.PERF_WEIGHT_WIN_RATE +
                       score_pr * settings.PERF_WEIGHT_PAYOFF_RATIO +
                       score_dd * settings.PERF_WEIGHT_DRAWDOWN)
        
        return max(0.0, min(1.0, final_score))

    def _save_state(self):
        state = {
            "total_profit": self.total_profit,
            "trades_history": self.trades_history,
            "equity_history": self.equity_history,
            "last_funding_fee_timestamp": self.last_funding_fee_timestamp
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            self.logger.error(f"保存业绩记录失败: {e}", exc_info=True)

    def load_state(self):
        if not os.path.exists(self.state_file):
            self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": self.initial_principal})
            return
        
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                self.total_profit = state.get("total_profit", 0.0)
                self.trades_history = state.get("trades_history", [])
                self.equity_history = state.get("equity_history", [])
                self.last_funding_fee_timestamp = state.get("last_funding_fee_timestamp", 0)
                self.is_new = False
                self.logger.info(f"成功从文件恢复业绩记录。")
                if not self.equity_history:
                    self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": self.initial_principal + self.total_profit})
        except Exception as e:
            self.logger.error(f"加载业绩记录失败: {e}", exc_info=True)
            self.is_new = True

    def add_profit(self, pnl: float):
        self.total_profit += pnl
        self.trades_history.append(pnl)
        new_equity = self.initial_principal + self.total_profit
        self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": new_equity})
        self.logger.info(f"新增一笔已实现盈亏: {pnl:+.4f} USDT | 累计总利润: {self.total_profit:.4f} USDT | 新净值: {new_equity:.4f} USDT")
        self._save_state()

    def add_funding_fees(self, fees: list):
        if not fees:
            return

        total_fee_amount = 0
        latest_timestamp = self.last_funding_fee_timestamp

        for fee in fees:
            if isinstance(fee, dict) and 'income' in fee and 'timestamp' in fee:
                fee_amount = float(fee['income'])
                self.total_profit += fee_amount
                total_fee_amount += fee_amount
                
                if fee['timestamp'] > latest_timestamp:
                    latest_timestamp = fee['timestamp']

        if total_fee_amount != 0:
            self.logger.warning(f"同步到 {len(fees)} 笔资金费用，共计: {total_fee_amount:+.4f} USDT。累计总利润更新为: {self.total_profit:.4f} USDT")
            new_equity = self.initial_principal + self.total_profit
            self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": new_equity})
        
        self.last_funding_fee_timestamp = latest_timestamp
        self._save_state()

    def get_total_profit(self) -> float:
        return self.total_profit

    def initialize_profit(self, initial_profit: float, trades_history: list = None):
        self.total_profit = initial_profit
        self.trades_history = trades_history if trades_history is not None else []
        
        self.equity_history = [{"timestamp": int(time.time() * 1000), "equity": self.initial_principal}]
        current_equity = self.initial_principal
        for pnl in self.trades_history:
            current_equity += pnl
            self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": current_equity})
            
        self.is_new = False
        self.logger.info(f"业绩记录已初始化，初始累计利润设置为: {self.total_profit:.4f} USDT")
        self._save_state()
