import logging
import os
import json
import time
from helpers import send_bark_notification

class PositionTracker:
    def __init__(self, symbol: str, state_dir: str):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.symbol}]")
        self.entries = []
        self.side = None
        self.stop_loss_price = 0.0
        self.take_profit_price = 0.0
        self.entry_reason = None
        self.initial_risk_per_unit = 0.0
        self.sl_stage = 1  # 止损阶段: 1=常规ATR追踪, 2=吊灯止损

        safe_symbol = symbol.replace('/', '_').replace(':', '_')
        self.state_file = os.path.join(state_dir, f'futures_position_{safe_symbol}.json')
        os.makedirs(state_dir, exist_ok=True)
        self.load_state()

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
        return sum(e['fee'] for e in self.entries)

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
            "sl_stage": self.sl_stage
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
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
            if self.is_position_open():
                self.logger.warning("！！！已从文件恢复一个未平仓头寸！！！")
                self.logger.info(f"恢复的头寸信息: {self.side.upper()} | {self.size:.8f} @ {self.entry_price:.4f} (均价) | 止损阶段: {self.sl_stage}")
        except Exception as e:
            self.logger.error(f"加载头寸状态失败: {e}", exc_info=True)

    def is_position_open(self) -> bool:
        return self.size > 0 and self.side is not None

    def open_position(self, side: str, entry_price: float, size: float, entry_fee: float, stop_loss: float, take_profit: float, timestamp: int, reason: str):
        self.side = side
        self.entries.append({'price': entry_price, 'size': size, 'fee': entry_fee, 'timestamp': timestamp})
        self.stop_loss_price, self.take_profit_price = stop_loss, take_profit
        self.entry_reason = reason
        self.initial_risk_per_unit = abs(entry_price - stop_loss)
        self.sl_stage = 1  # 每次开仓时，重置为阶段1
        self.logger.info(f"====== 新建头寸 (原因: {reason}) ======\n方向: {side.upper()} | 数量: {size:.8f} | 价格: {entry_price}\n初始风险(1R)锁定为: {self.initial_risk_per_unit:.4f}")
        self._save_state()

    def add_to_position(self, entry_price: float, size: float, entry_fee: float, timestamp: int):
        if not self.is_position_open():
            return
        self.entries.append({'price': entry_price, 'size': size, 'fee': entry_fee, 'timestamp': timestamp})
        self._save_state()

    def close_position(self):
        self.side = None
        self.entries = []
        self.stop_loss_price, self.take_profit_price = 0.0, 0.0
        self.entry_reason = None
        self.initial_risk_per_unit = 0.0
        self.sl_stage = 1  # 平仓后，重置为阶段1
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
            "sl_stage": self.sl_stage
        }

    def advance_sl_stage(self, new_stage: int):
        if self.sl_stage < new_stage:
            self.logger.warning(f"止损系统升级！从阶段 {self.sl_stage} -> {new_stage}。")
            self.sl_stage = new_stage
            self._save_state()

    def update_stop_loss(self, new_stop_loss: float, reason: str = "Unknown"):
        if not self.is_position_open():
            return

        old_sl = self.stop_loss_price

        reason_map = {
            "ATR Trailing": "🚀",
            "Defensive Adjustment": "🛡️",
            "Pyramiding Secure": "➕",
            "Chandelier Exit": "💡"
        }
        emoji = reason_map.get(reason, "⚙️")

        if self.side == 'long' and new_stop_loss > self.stop_loss_price:
            log_msg = f"{emoji} {reason}: 多头止损上移: 从 {old_sl:.4f} -> {new_stop_loss:.4f}"
            self.logger.info(log_msg)
            self.stop_loss_price = new_stop_loss
            self._save_state()

            title = f"{emoji} {self.symbol} 止损位上移"
            content = f"原因: {reason}\n方向: {self.side.upper()}\n旧止损: {old_sl:.4f}\n新止损: {new_stop_loss:.4f}"
            send_bark_notification(content, title)

        elif self.side == 'short' and new_stop_loss < self.stop_loss_price:
            log_msg = f"{emoji} {reason}: 空头止损下移: 从 {old_sl:.4f} -> {new_stop_loss:.4f}"
            self.logger.info(log_msg)
            self.stop_loss_price = new_stop_loss
            self._save_state()

            title = f"{emoji} {self.symbol} 止损位下移"
            content = f"原因: {reason}\n方向: {self.side.upper()}\n旧止损: {old_sl:.4f}\n新止损: {new_stop_loss:.4f}"
            send_bark_notification(content, title)
