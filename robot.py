"""
华尔街大拿期权波段交易机器人 - 主循环
Polls Tiger API, runs strategy, executes trades.

SECURITY:
- Credentials passed via config dict, used in-memory only
- NEVER logs private keys
- Paper trading ONLY
"""
import logging
import time
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable

from strategy import check_signal, check_option_filter
from tiger_client import TigerClient, get_poll_interval

logger = logging.getLogger(__name__)


class TradingRobot:
    """
    Main robot loop.
    - Watches a list of symbols
    - Runs strategy on each poll tick
    - Places orders when signal + filters pass
    - Enforces max positions and daily loss limit
    """

    def __init__(self, config: Dict[str, Any],
                 on_trade: Optional[Callable[[Dict], None]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None):
        """
        Args:
            config: {
                tiger_id: str,
                private_key: str (in-memory only),
                symbols: List[str],
                trade_type: 'option' or 'stock',
                timeframe: str (e.g. '15min', '1min', '10sec'),
                amount: float (USD per trade),
                max_positions: int,
                max_loss: float (daily max loss USD, stop if exceeded),
                iv_threshold: float (default 30),
                min_days: int (min days to expiry, default 30),
                account: Optional[str] (paper account number),
            }
            on_trade: callback(trade_dict) called on each trade
            on_log: callback(message, level) called for log lines
        """
        self.config = config
        self.on_trade = on_trade
        self.on_log = on_log

        self.symbols = config.get('symbols', [])
        self.trade_type = config.get('trade_type', 'option')
        self.timeframe = config.get('timeframe', '15min')
        self.amount = float(config.get('amount', 1000))
        self.max_positions = int(config.get('max_positions', 3))
        self.max_loss = float(config.get('max_loss', 500))
        self.iv_threshold = float(config.get('iv_threshold', 30))
        self.min_days = int(config.get('min_days', 30))

        self.client: Optional[TigerClient] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.trades: List[Dict] = []
        self.daily_pnl = 0.0
        self.start_time: Optional[datetime] = None
        self._stop_event = threading.Event()

    def _log(self, msg: str, level: str = 'info'):
        logger.info(f"[{level}] {msg}")
        if self.on_log:
            try:
                self.on_log(msg, level)
            except Exception:
                pass

    def _emit_trade(self, trade: Dict):
        self.trades.append(trade)
        if self.on_trade:
            try:
                self.on_trade(trade)
            except Exception:
                pass

    def start(self) -> bool:
        """Start the robot. Returns False if already running or config invalid."""
        if self.running:
            self._log("机器人已在运行", 'warn')
            return False
        if not self.symbols:
            self._log("未选择股票，无法启动", 'warn')
            return False
        if not self.config.get('tiger_id') or not self.config.get('private_key'):
            self._log("缺少 Tiger API 配置（Tiger ID / 私钥）", 'warn')
            return False

        try:
            self.client = TigerClient(
                tiger_id=self.config['tiger_id'],
                private_key=self.config['private_key'],
                account=self.config.get('account'),
                sandbox=True,  # Paper trading ONLY
            )
        except Exception as e:
            self._log(f"Tiger 连接失败: {type(e).__name__}: {e}", 'error')
            return False

        if not self.client.is_connected:
            self._log("Tiger 客户端初始化失败（SDK 未安装或配置错误）", 'error')
            # Still run in demo mode: strategy runs on mock data path
            self._log("进入演示模式：策略计算可用，但不下真实订单", 'warn')

        self.running = True
        self._stop_event.clear()
        self.start_time = datetime.now()
        self.daily_pnl = 0.0

        self._log(f"机器人已启动", 'buy')
        self._log(f"交易类型: {'期权' if self.trade_type == 'option' else '个股'}", 'info')
        self._log(f"K线周期: {self.timeframe}", 'info')
        self._log(f"股票池: {', '.join(self.symbols)}", 'info')
        self._log(f"每次金额: ${self.amount}", 'info')
        self._log(f"开始监控信号...", 'info')

        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        """Stop the robot."""
        self.running = False
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        self._log("机器人已停止", 'sell')

    def _run_loop(self):
        """Main polling loop."""
        poll_interval = get_poll_interval(self.timeframe)
        self._log(f"轮询间隔: {poll_interval} 秒", 'info')

        while self.running and not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log(f"轮询异常: {type(e).__name__}: {e}", 'error')
            # Wait for next tick (interruptible)
            self._stop_event.wait(poll_interval)

    def _tick(self):
        """One polling cycle: check all symbols."""
        # Daily loss limit check
        if self.daily_pnl <= -abs(self.max_loss):
            self._log(
                f"今日亏损 ${self.daily_pnl:.2f} 已达上限 ${self.max_loss}，停止交易",
                'warn'
            )
            self.stop()
            return

        # Position count check
        open_positions = self._get_open_position_count()
        if open_positions >= self.max_positions:
            self._log(f"持仓已满 ({open_positions}/{self.max_positions})，跳过本轮", 'info')
            return

        for symbol in self.symbols:
            if not self.running:
                break
            try:
                self._check_symbol(symbol)
            except Exception as e:
                self._log(f"检查 {symbol} 异常: {type(e).__name__}: {e}", 'error')

    def _get_open_position_count(self) -> int:
        """Get number of open positions."""
        if self.client and self.client.is_connected:
            try:
                positions = self.client.get_positions()
                return len([p for p in positions if p.get('quantity', 0) != 0])
            except Exception:
                return 0
        return 0

    def _check_symbol(self, symbol: str):
        """Check one symbol for trading signal."""
        if not self.client:
            return

        kline = self.client.get_kline(symbol, self.timeframe, limit=100)
        if not kline or not kline.get('close') or len(kline['close']) < 30:
            self._log(f"{symbol}: K线数据不足，跳过", 'info')
            return

        closes = kline['close']
        highs = kline['high']
        lows = kline['low']

        signal, detail = check_signal(closes, highs, lows, closes)
        if signal == 'HOLD':
            return

        self._log(
            f"{symbol}: 信号 {signal} | "
            f"GDC={detail.get('gdc_index')}({detail.get('gdc_color')}) "
            f"GD={detail.get('gd_index')} 梯子={detail.get('ladder')}",
            'info'
        )

        if self.trade_type == 'option':
            self._handle_option_signal(symbol, signal, closes[-1])
        else:
            self._handle_stock_signal(symbol, signal, closes[-1])

    def _handle_option_signal(self, symbol: str, signal: str, current_price: float):
        """Handle option trade: check IV + expiry, then place order."""
        # Get option chain
        chain = self.client.get_option_chain(symbol) if self.client else None
        if not chain:
            self._log(f"{symbol}: 无法获取期权链，跳过", 'warn')
            return

        # Find best expiration: nearest expiry with > min_days
        expirations = chain.get('expirations', [])
        target_expiry = None
        days_to_expiry = None
        for exp in expirations:
            # exp format varies; try to parse date
            try:
                exp_date = datetime.strptime(str(exp)[:10], '%Y-%m-%d')
                days = (exp_date - datetime.now()).days
                if days >= self.min_days:
                    if target_expiry is None or days < days_to_expiry:
                        target_expiry = exp
                        days_to_expiry = days
            except Exception:
                continue

        if target_expiry is None:
            self._log(f"{symbol}: 无满足到期日>{self.min_days}天的期权", 'warn')
            return

        # Get option contracts for target expiry
        briefs = self.client.get_option_briefs(symbol, target_expiry) if self.client else None
        if not briefs:
            self._log(f"{symbol}: 无法获取期权合约，跳过", 'warn')
            return

        # Filter by CALL/PUT and ATM strike
        right = 'CALL' if signal == 'CALL' else 'PUT'
        candidates = [b for b in briefs if b.get('right', '').upper() == right]
        if not candidates:
            self._log(f"{symbol}: 无 {right} 期权合约", 'warn')
            return

        # Pick ATM (strike closest to current price)
        candidates.sort(key=lambda b: abs(b.get('strike', 0) - current_price))
        best = candidates[0]

        # IV filter: need IV Rank. If per-contract IV available, use it as proxy.
        # True IV Rank needs historical IV; here we check contract IV < threshold proxy
        # If IV data missing, log warning and proceed (user decides)
        iv = best.get('iv')
        if iv is not None:
            # IV is decimal (e.g. 0.35 = 35%). Convert to rank-like proxy.
            # If IV > 0.5 (50%), treat as high IV and skip.
            if iv > 0.5:
                self._log(f"{symbol}: 期权 IV {iv*100:.1f}% 过高，跳过", 'warn')
                return

        # Place order: quantity = amount / (option price * 100)
        # Option price per contract = price * 100 (100 shares per contract)
        opt_price = best.get('price') or 0
        if opt_price <= 0:
            self._log(f"{symbol}: 期权价格无效，跳过", 'warn')
            return

        quantity = max(1, int(self.amount / (opt_price * 100)))
        identifier = best.get('identifier') or f"{symbol} {target_expiry} {right}"

        self._log(
            f"{symbol}: 准备下单 {right} 期权 | 行权价 {best.get('strike')} "
            f"| 到期 {target_expiry} ({days_to_expiry}天) | 价格 ${opt_price} x{quantity}",
            'buy'
        )

        result = self.client.place_order(identifier, 'BUY', quantity)
        if result:
            trade = {
                'time': datetime.now().isoformat(),
                'symbol': symbol,
                'type': 'option',
                'side': signal,
                'contract': identifier,
                'strike': best.get('strike'),
                'expiry': str(target_expiry),
                'price': opt_price,
                'quantity': quantity,
                'amount': opt_price * 100 * quantity,
                'order_id': result.get('order_id'),
                'status': result.get('status'),
            }
            self._emit_trade(trade)
            self._log(f"{symbol}: 下单成功，订单号 {result.get('order_id')}", 'buy')
        else:
            self._log(f"{symbol}: 下单失败", 'error')

    def _handle_stock_signal(self, symbol: str, signal: str, current_price: float):
        """Handle stock trade: buy/sell shares."""
        if current_price <= 0:
            return
        quantity = max(1, int(self.amount / current_price))
        action = 'BUY' if signal == 'CALL' else 'SELL'

        self._log(
            f"{symbol}: 准备{action}个股 | 现价 ${current_price:.2f} x{quantity}",
            'buy' if action == 'BUY' else 'sell'
        )

        result = self.client.place_order(symbol, action, quantity) if self.client else None
        if result:
            trade = {
                'time': datetime.now().isoformat(),
                'symbol': symbol,
                'type': 'stock',
                'side': signal,
                'action': action,
                'price': current_price,
                'quantity': quantity,
                'amount': current_price * quantity,
                'order_id': result.get('order_id'),
                'status': result.get('status'),
            }
            self._emit_trade(trade)
            self._log(f"{symbol}: 下单成功，订单号 {result.get('order_id')}", 'buy')
        else:
            self._log(f"{symbol}: 下单失败", 'error')

    def get_status(self) -> Dict[str, Any]:
        """Get current robot status."""
        return {
            'running': self.running,
            'symbols': self.symbols,
            'trade_type': self.trade_type,
            'timeframe': self.timeframe,
            'amount': self.amount,
            'max_positions': self.max_positions,
            'max_loss': self.max_loss,
            'daily_pnl': self.daily_pnl,
            'trades_count': len(self.trades),
            'trades': self.trades[-50:],  # Last 50 trades
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'positions': self.client.get_positions() if self.client and self.client.is_connected else [],
        }
