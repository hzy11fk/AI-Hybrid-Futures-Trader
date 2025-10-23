# 文件: position_tracker.py (V8 - 增加突破动能模式的状态管理)
import logging
import os
import json
from helpers import send_bark_notification

class PositionTracker:
    def __init__(self, symbol: str, state_dir: str):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.symbol}]")
        self.state_file = self._get_state_file_path(symbol, state_dir)
        
        # --- 默认状态属性 ---
        self.side = None
        self.entries = []
        self.stop_loss_price = 0.0
        self.take_profit_price = 0.0
        self.entry_reason = None # 关键：用于区分交易模式
        self.initial_risk_per_unit = 0.0
        self.sl_stage = 1
        self.partial_tp_counter = 0
        
        # [新增] 用于突破动能交易的价格极值记录
        self.high_water_mark = 0.0 # 多单使用，记录最高价
        self.low_water_mark = float('inf') # 空单使用，记录最低价

        os.makedirs(state_dir, exist_ok=True)
        self.load_state()

    def _get_state_file_path(self, symbol, state_dir):
        safe_symbol = symbol.replace('/', '_').replace(':', '_')
        return os.path.join(state_dir, f'futures_position_{safe_symbol}.json')

    @property
    def size(self) -> float:
        if not self.entries: return 0.0
        return sum(e['size'] for e in self.entries)

    @property
    def entry_price(self) -> float:
        if not self.entries: return 0.0
        total_size = self.size
        if total_size == 0: return 0.0
        return sum(e['price'] * e['size'] for e in self.entries) / total_size

    @property
    def entry_fee(self) -> float:
        if not self.entries: return 0.0
        return sum(e.get('fee', 0.0) for e in self.entries)

    @property
    def break_even_price(self) -> float:
        if not self.is_position_open(): return 0.0
        total_size = self.size
        if total_size == 0: return 0.0
        total_value = sum(e['price'] * e['size'] for e in self.entries)
        total_fees = self.entry_fee
        if self.side == 'long': return (total_value + total_fees) / total_size
        elif self.side == 'short': return (total_value - total_fees) / total_size
        return 0.0

    def _save_state(self):
        state = {
            "side": self.side,
            "entries": self.entries,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "entry_reason": self.entry_reason,
            "initial_risk_per_unit": self.initial_risk_per_unit,
            "sl_stage": self.sl_stage,
            "partial_tp_counter": self.partial_tp_counter,
            "high_water_mark": self.high_water_mark, # 保存价格极值
            "low_water_mark": self.low_water_mark    # 保存价格极值
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=4)
        except Exception as e:
            self.logger.error(f"保存头寸状态失败: {e}", exc_info=True)

    def load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            self.side = state.get("side")
            self.entries = state.get("entries", [])
            self.stop_loss_price = state.get("stop_loss_price", 0.0)
            self.take_profit_price = state.get("take_profit_price", 0.0)
            self.entry_reason = state.get("entry_reason")
            self.initial_risk_per_unit = state.get("initial_risk_per_unit", 0.0)
            self.sl_stage = state.get("sl_stage", 1)
            self.partial_tp_counter = state.get("partial_tp_counter", 0)
            self.high_water_mark = state.get("high_water_mark", 0.0) # 加载价格极值
            self.low_water_mark = state.get("low_water_mark", float('inf')) # 加载价格极值
            if self.is_position_open():
                self.logger.warning("！！！已从文件恢复一个未平仓头寸！！！")
        except Exception as e:
            self.logger.error(f"加载头寸状态失败: {e}", exc_info=True)

    def is_position_open(self) -> bool:
        return self.size > 0 and self.side is not None

    def open_position(self, side: str, entry_price: float, size: float, entry_fee: float, stop_loss: float, take_profit: float, timestamp: int, reason: str):
        self.side = side
        self.entries = [{'price': entry_price, 'size': size, 'fee': entry_fee, 'timestamp': timestamp}]
        self.stop_loss_price, self.take_profit_price = stop_loss, take_profit
        self.entry_reason = reason
        self.initial_risk_per_unit = abs(entry_price - stop_loss)
        self.sl_stage = 1
        self.partial_tp_counter = 0
        
        # 如果是突破动能交易，则初始化价格极值记录
        if reason == 'breakout_momentum_trade':
            self.high_water_mark = entry_price
            self.low_water_mark = entry_price
        else:
            self.high_water_mark = 0.0
            self.low_water_mark = float('inf')

        self.logger.info(f"====== 新建头寸 (原因: {reason}) ======\n方向: {side.upper()} | 数量: {size:.8f} | 价格: {entry_price}\n初始风险(1R)锁定为: {self.initial_risk_per_unit:.4f}")
        self._save_state()

    def add_to_position(self, entry_price: float, size: float, entry_fee: float, timestamp: int):
        if not self.is_position_open(): return
        self.entries.append({'price': entry_price, 'size': size, 'fee': entry_fee, 'timestamp': timestamp})
        self._save_state()

    def close_position(self):
        self.side = None
        self.entries = []
        self.stop_loss_price, self.take_profit_price = 0.0, 0.0
        self.entry_reason = None
        self.initial_risk_per_unit = 0.0
        self.sl_stage = 1
        self.partial_tp_counter = 0
        # 重置价格极值记录
        self.high_water_mark = 0.0
        self.low_water_mark = float('inf')
        self._save_state()

    def get_status(self):
        return {
            "is_open": self.is_position_open(),
            "side": self.side,
            "entry_price": self.entry_price,
            "size": self.size,
            "entry_fee": self.entry_fee,
            "stop_loss": self.stop_loss_price,
            "take_profit": self.take_profit_price,
            "add_count": len(self.entries) - 1 if self.is_position_open() else -1,
            "entries": self.entries,
            "entry_reason": self.entry_reason,
            "initial_risk_per_unit": self.initial_risk_per_unit,
            "sl_stage": self.sl_stage,
            "partial_tp_counter": self.partial_tp_counter,
            "high_water_mark": self.high_water_mark,
            "low_water_mark": self.low_water_mark
        }

    def update_price_mark(self, current_price: float):
        """更新价格极值记录 (最高/最低价)"""
        updated = False
        if self.side == 'long' and current_price > self.high_water_mark:
            self.high_water_mark = current_price
            updated = True
        elif self.side == 'short' and current_price < self.low_water_mark:
            self.low_water_mark = current_price
            updated = True
        
        if updated:
            self._save_state()

    def update_stop_loss(self, new_stop_loss: float, reason: str = "Unknown") -> bool:
        if not self.is_position_open(): return False
        old_sl = self.stop_loss_price
        updated = False
        reason_map = {"ATR Trailing": "🚀", "Defensive Adjustment": "🛡️", "Pyramiding Secure": "➕", "Chandelier Exit": "💡", "Secure after Partial TP": "🔒", "Move SL to Breakeven": " ब्रेक_even", "Breakout Momentum Trail": "⚡️"}
        emoji = reason_map.get(reason, "⚙️")
        if self.side == 'long' and new_stop_loss > self.stop_loss_price:
            self.stop_loss_price = new_stop_loss
            updated = True
        elif self.side == 'short' and new_stop_loss < self.stop_loss_price and (self.stop_loss_price > 0 or new_stop_loss < 0):
            self.stop_loss_price = new_stop_loss
            updated = True
        if updated:
            self._save_state()
            title = f"{emoji} {self.symbol} 止损位更新"
            content = f"原因: {reason}\n旧止损: {old_sl:.4f}\n新止损: {self.stop_loss_price:.4f}"
            send_bark_notification(content, title)
        return updated

    def increment_partial_tp_counter(self):
        self.partial_tp_counter += 1
        self.logger.info(f"阶段性部分止盈计数器已更新为: {self.partial_tp_counter}")
        self._save_state()

    def reset_partial_tp_counter(self, reason: str):
        if self.partial_tp_counter != 0:
            self.partial_tp_counter = 0
            self.logger.warning(f"阶段性部分止盈计数器已重置为0，原因: {reason}")
            self._save_state()
           
    def advance_sl_stage(self, new_stage: int):
        if self.sl_stage < new_stage:
            self.logger.warning(f"止损系统升级！从阶段 {self.sl_stage} -> {new_stage}。")
            self.sl_stage = new_stage
            self._save_state()
           
    def handle_partial_close(self, closed_size: float):
        if not self.is_position_open(): return
        current_total_size = self.size
        if closed_size >= current_total_size:
            self.close_position()
            return
        ratio = (current_total_size - closed_size) / current_total_size
        for entry in self.entries:
            entry['size'] *= ratio
        self.logger.info(f"部分平仓后，仓位数量从 {current_total_size:.8f} 更新为 {self.size:.8f}")
        self._save_state()
