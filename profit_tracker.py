# 文件: profit_tracker.py (V5 - 最终版)

import logging
import os
import json
import time
import math
from config import settings # 确保导入settings

class ProfitTracker:
    """
    V5 - 升级版业绩记录器，存储完整的交易字典历史记录。
    """
    def __init__(self, state_dir: str, symbol: str, initial_principal: float):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.symbol}]")
        self.initial_principal = initial_principal
        
        self.total_profit = 0.0
        self.trades_history = [] # 现在存储的是完整的交易字典列表
        self.equity_history = []
        self.last_funding_fee_timestamp = 0

        self.is_new = True
        safe_symbol = symbol.replace('/', '_').replace(':', '_')
        
        # 强制使用传入的 state_dir 参数构建路径
        self.state_file = os.path.join(state_dir, f'futures_profit_tracker_{safe_symbol}.json')
        
        # 确保创建的是传入的 state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.load_state()

    @property
    def win_rate(self) -> float | None:
        """计算胜率"""
        total_trades = len(self.trades_history)
        if total_trades == 0:
            return None
        # 从字典中获取net_pnl来判断胜负
        winning_trades = len([t for t in self.trades_history if t.get('net_pnl', 0) > 0])
        return (winning_trades / total_trades) * 100

    @property
    def payoff_ratio(self) -> float | None:
        """计算盈亏比"""
        # 从字典中获取net_pnl来计算
        wins = [t['net_pnl'] for t in self.trades_history if t.get('net_pnl', 0) > 0]
        losses = [t['net_pnl'] for t in self.trades_history if t.get('net_pnl', 0) < 0]

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
            # 确保 peak 不为 0 或负数
            drawdown = (peak - equity) / peak if peak > 0 else 0.0
            if drawdown > max_dd:
                max_dd = drawdown
        
        return max_dd * 100

    def get_performance_score(self) -> float | None:
        """计算综合表现得分"""
        if len(self.trades_history) < settings.MIN_TRADES_FOR_EVALUATION:
            return None

        win_rate_val = self.win_rate
        payoff_ratio_val = self.payoff_ratio
        max_drawdown_val = self.max_drawdown

        if win_rate_val is None or payoff_ratio_val is None or max_drawdown_val is None:
            return None

        # 使用健壮的计算，避免 math domain error
        score_wr = win_rate_val / 100.0 if win_rate_val is not None else 0.0
        # 限制 payoff_ratio_val 避免溢出
        pr_val = max(-5, min(5, payoff_ratio_val - 1.5)) if payoff_ratio_val is not None else 0.0
        score_pr = 1 / (1 + math.exp(-2 * pr_val))
        score_dd = 1.0 - (max_drawdown_val / 100.0) if max_drawdown_val is not None else 0.0

        final_score = (score_wr * settings.PERF_WEIGHT_WIN_RATE +
                       score_pr * settings.PERF_WEIGHT_PAYOFF_RATIO +
                       score_dd * settings.PERF_WEIGHT_DRAWDOWN)
        
        return max(0.0, min(1.0, final_score)) # 确保得分在 0 和 1 之间

    def _save_state(self):
        """保存当前状态到JSON文件。"""
        state = {
            "total_profit": self.total_profit,
            "trades_history": self.trades_history,
            "equity_history": self.equity_history,
            "last_funding_fee_timestamp": self.last_funding_fee_timestamp
        }
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f: # 指定编码
                json.dump(state, f, indent=4)
        except Exception as e:
            self.logger.error(f"保存业绩记录失败: {e}", exc_info=True)

    def load_state(self):
        """从JSON文件加载状态。"""
        if not os.path.exists(self.state_file):
            self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": self.initial_principal})
            self.logger.info("未找到业绩记录文件，将创建新的记录。")
            return
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f: # 指定编码
                state = json.load(f)
            self.total_profit = state.get("total_profit", 0.0)
            self.trades_history = state.get("trades_history", [])
            self.equity_history = state.get("equity_history", [])
            self.last_funding_fee_timestamp = state.get("last_funding_fee_timestamp", 0)
            self.is_new = False
            self.logger.info(f"成功从文件恢复业绩记录。")
            # 如果加载后 equity_history 为空，初始化它
            if not self.equity_history:
                self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": self.initial_principal + self.total_profit})
        except json.JSONDecodeError as e:
             self.logger.error(f"加载业绩记录失败：JSON文件格式错误 - {e}", exc_info=True)
             self._handle_load_error()
        except Exception as e:
            self.logger.error(f"加载业绩记录失败: {e}", exc_info=True)
            self._handle_load_error()

    def _handle_load_error(self):
         """处理加载状态失败的情况，备份旧文件并初始化。"""
         self.logger.warning("将尝试备份损坏的业绩文件并重新初始化。")
         try:
             backup_path = self.state_file + f".backup_{int(time.time())}"
             os.rename(self.state_file, backup_path)
             self.logger.info(f"损坏的文件已备份至: {backup_path}")
         except Exception as rename_e:
             self.logger.error(f"备份损坏的业绩文件失败: {rename_e}")
         
         # 重置状态为初始状态
         self.total_profit = 0.0
         self.trades_history = []
         self.equity_history = [{"timestamp": int(time.time() * 1000), "equity": self.initial_principal}]
         self.last_funding_fee_timestamp = 0
         self.is_new = True
         self._save_state() # 保存初始状态


    def record_trade(self, trade_data: dict):
        """
        [核心方法] 记录一笔完整的交易。
        代替旧的 add_profit 方法。
        """
        # 安全地获取 net_pnl，如果不存在或无效，默认为 0.0
        net_pnl = trade_data.get('net_pnl')
        if not isinstance(net_pnl, (int, float)):
             self.logger.warning(f"接收到的交易记录缺少有效的 net_pnl: {trade_data}")
             net_pnl = 0.0
             
        self.total_profit += net_pnl
        self.trades_history.append(trade_data) # 存入完整的交易字典
        
        new_equity = self.initial_principal + self.total_profit
        self.equity_history.append({"timestamp": int(time.time() * 1000), "equity": new_equity})
        
        self.logger.info(f"记录一笔已实现交易: 盈亏 {net_pnl:+.4f} USDT | 累计总利润: {self.total_profit:.4f} USDT | 新净值: {new_equity:.4f} USDT")
        self._save_state()

    def add_funding_fees(self, fees: list):
        """同步资金费用。"""
        # 此函数无需修改
        if not fees: return
        total_fee_amount, latest_timestamp = 0.0, self.last_funding_fee_timestamp
        valid_fees_processed = 0
        for fee in fees:
            # 增加更严格的检查
            if isinstance(fee, dict) and 'income' in fee and 'timestamp' in fee and fee.get('asset') == 'USDT':
                try:
                    fee_amount = float(fee['income'])
                    fee_ts = int(fee['timestamp'])
                    
                    # 确保只处理比上次记录更新的费用
                    if fee_ts > self.last_funding_fee_timestamp:
                         self.total_profit += fee_amount
                         total_fee_amount += fee_amount
                         if fee_ts > latest_timestamp: latest_timestamp = fee_ts
                         valid_fees_processed += 1
                         
                except (ValueError, TypeError) as e:
                     self.logger.warning(f"处理资金费用记录时遇到无效数据: {fee}, 错误: {e}")
                     
        if valid_fees_processed > 0:
            self.logger.info(f"同步到 {valid_fees_processed} 笔新的资金费用，共计: {total_fee_amount:+.4f} USDT。累计总利润更新为: {self.total_profit:.4f} USDT")
            new_equity = self.initial_principal + self.total_profit
            # 使用 latest_timestamp 而不是 time.time() 来记录权益点，更准确反映资金费用发生的时间点
            self.equity_history.append({"timestamp": latest_timestamp, "equity": new_equity})
            # 按时间戳排序 equity_history，确保图表正确
            self.equity_history.sort(key=lambda x: x['timestamp'])
        else:
             self.logger.info("未发现需要同步的新的资金费用记录。")

        self.last_funding_fee_timestamp = latest_timestamp
        self._save_state()

    def get_total_profit(self) -> float:
        """获取当前累计的总利润。"""
        return self.total_profit
