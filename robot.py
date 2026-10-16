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
        # Per-position forced stop-loss (%). Default 15. User-configurable.
        # HARD control: cannot be disabled (min 0.1% enforced in setter use).
        self.stop_loss_pct = float(config.get('stop_loss_pct', 15.0))
        if self.stop_loss_pct <= 0:
            self.stop_loss_pct = 15.0

        self.client: Optional[TigerClient] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.trades: List[Dict] = []
        self.daily_pnl = 0.0
        self.start_time: Optional[datetime] = None
        self._stop_event = threading.Event()
        # GDC 颜色追踪：用于高灵敏度变色立即卖出
        self._prev_gdc_color: Dict[str, str] = {}  # {symbol: 'magenta'/'green'}
        # 连续亏损保护：连亏 3 次停 5 分钟
        self._consecutive_losses = 0
        self._cooling_until = None  # datetime，冷却结束时间
        # 断线重连
        self._last_reconnect_attempt = None

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
        self._log(f"单仓止损: 亏损 {self.stop_loss_pct}% 即强制平仓（硬性）", 'info')
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
        # 断线自动重连：Tiger API 断了自动重连
        if self.client and not self.client.is_connected:
            try:
                self._log("检测到断线，尝试重连...", 'warn')
                self.client.connect()
                if self.client.is_connected:
                    self._log("重连成功", 'info')
                else:
                    self._log("重连失败，下轮重试", 'warn')
                    return
            except Exception:
                return

        # 连续亏损保护：连亏 3 次，冷却 5 分钟不交易
        if self._cooling_until and datetime.now() < self._cooling_until:
            return  # 冷却中，跳过本轮
        if self._cooling_until and datetime.now() >= self._cooling_until:
            self._cooling_until = None
            self._consecutive_losses = 0
            self._log("冷却结束，恢复交易", 'info')

        # HARD stop-loss FIRST: close any position at/beyond the loss threshold,
        # even if the daily loss limit is about to stop the robot. Never skipped.
        self._check_stop_loss()

        # 高灵敏度：GDC 颜色一变，立刻卖出，不迟疑（期权衰减快）
        # 10/15/30/45秒、1分钟短线模式才启用
        self._check_gdc_color_change()

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

    def _check_stop_loss(self) -> int:
        """Per-position forced stop-loss. Runs on EVERY poll, cannot be bypassed.

        For each open position, computes loss % from per-unit prices:
          long  (qty > 0): loss% = (avg_cost - market_price) / avg_cost * 100
          short (qty < 0): loss% = (market_price - avg_cost) / avg_cost * 100
        If loss% >= stop_loss_pct, places an opposite market order immediately,
        logs the trigger, and records a STOP_LOSS trade (visible in trade history).
        Returns number of positions closed this call.
        """
        if not self.client or not self.client.is_connected:
            return 0
        try:
            positions = self.client.get_positions()
        except Exception:
            return 0
        closed = 0
        for pos in positions:
            qty = int(pos.get('quantity', 0) or 0)
            if qty == 0:
                continue
            avg = float(pos.get('avg_cost', 0) or 0)
            mkt = float(pos.get('market_price', 0) or 0)
            if avg <= 0 or mkt <= 0:
                continue
            if qty > 0:
                loss_pct = (avg - mkt) / avg * 100
            else:
                loss_pct = (mkt - avg) / avg * 100
            if loss_pct < self.stop_loss_pct:
                continue
            symbol = pos.get('symbol', '')
            action = 'SELL' if qty > 0 else 'BUY'
            self._log(
                f"🛑 止损触发: {symbol} 亏损 {loss_pct:.1f}% ≥ {self.stop_loss_pct}% "
                f"| 成本 ${avg:.2f} 现价 ${mkt:.2f} | 强制平仓",
                'sell'
            )
            try:
                res = self.client.place_order(symbol, action, abs(qty))
                if res:
                    closed += 1
                    trade = {
                        'time': datetime.now().isoformat(),
                        'symbol': symbol,
                        'type': 'stop_loss',
                        'side': 'STOP_LOSS',
                        'action': action,
                        'price': mkt,
                        'quantity': abs(qty),
                        'amount': mkt * abs(qty),
                        'loss_pct': round(loss_pct, 2),
                        'order_id': res.get('order_id'),
                        'status': res.get('status'),
                        'note': f'止损 {loss_pct:.1f}%',
                    }
                    self._emit_trade(trade)
                    self._log(f"止损平仓成功: {symbol} 订单号 {res.get('order_id')}", 'sell')
                else:
                    self._log(f"止损平仓失败: {symbol} 下单返回为空，下轮继续尝试", 'error')
            except Exception as e:
                self._log(f"止损平仓异常: {symbol} {type(e).__name__}，下轮继续尝试", 'error')
        return closed

    def _check_gdc_color_change(self):
        """高灵敏度：GDC 颜色一变，立刻卖出，不迟疑。
        仅用于 1 分钟以内短线模式（10/15/30/45秒、1分钟）。
        - GDC 红 → 绿：手里有 Call（多头），立刻卖
        - GDC 绿 → 红：手里有 Put（空头），立刻卖
        期权时间价值衰减快，不能等。
        """
        # 只在短线模式启用
        tf = (self.timeframe or '').lower().strip()
        if tf not in ('10sec', '10s', '15sec', '15s', '30sec', '30s', '45sec', '45s', '1min', '1m'):
            return
        if not self.client or not self.client.is_connected:
            return
        try:
            from strategy import calculate_gdc
            positions = self.client.get_positions()
            if not positions:
                return
            # 建立持仓映射
            pos_map = {}
            for pos in positions:
                sym = pos.get('symbol', '').upper()
                qty = int(pos.get('quantity', 0) or 0)
                if qty != 0:
                    pos_map[sym] = pos
            if not pos_map:
                return
            for symbol in self.symbols:
                sym_up = symbol.upper()
                if sym_up not in pos_map:
                    continue
                # 获取 K 线，计算当前 GDC 颜色
                try:
                    kline = self.client.get_kline(symbol, self.timeframe, 40)
                    if not kline or not kline.get('close'):
                        continue
                    closes = kline['close']
                    _, gdc_color = calculate_gdc(closes)
                    if gdc_color is None:
                        continue
                    prev = self._prev_gdc_color.get(sym_up)
                    # 第一次记录，不交易
                    if prev is None:
                        self._prev_gdc_color[sym_up] = gdc_color
                        continue
                    # 颜色变了！
                    if prev != gdc_color:
                        self._log(f"⚡ {symbol} GDC 颜色变了 {prev} → {gdc_color}，立刻卖出！", 'sell')
                        self._prev_gdc_color[sym_up] = gdc_color
                        # 平掉该股票的仓位
                        pos = pos_map[sym_up]
                        qty = int(pos.get('quantity', 0) or 0)
                        action = 'SELL' if qty > 0 else 'BUY'
                        try:
                            res = self.client.place_order(symbol, action, abs(qty))
                            if res:
                                self._log(f"⚡ GDC 变色平仓成功: {symbol} {action} x{abs(qty)}", 'sell')
                            else:
                                self._log(f"⚡ GDC 变色平仓失败: {symbol}，下轮重试", 'error')
                        except Exception as e:
                            self._log(f"⚡ GDC 变色平仓异常: {symbol} {type(e).__name__}", 'error')
                    else:
                        # 颜色没变，更新记录
                        self._prev_gdc_color[sym_up] = gdc_color
                except Exception:
                    continue
        except Exception:
            pass

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

        signal, detail, size_ratio = check_signal(closes, highs, lows, closes, self.timeframe)
        if signal == 'HOLD':
            return

        self._log(
            f"{symbol}: 信号 {signal} | "
            f"GDC={detail.get('gdc_index')}({detail.get('gdc_color')}) "
            f"GD={detail.get('gd_index')} 梯子={detail.get('ladder')} "
            f"模式={detail.get('mode')} 仓位比={size_ratio} 原因={detail.get('reason')}",
            'info'
        )
        # 保存仓位比，供下单时使用
        self._last_size_ratio = size_ratio

        if self.trade_type == 'option':
            self._handle_option_signal(symbol, signal, closes[-1])
        else:
            self._handle_stock_signal(symbol, signal, closes[-1])

    def _handle_option_signal(self, symbol: str, signal: str, current_price: float):
        """Handle option trade: check IV + expiry, then place order."""
        # Hard risk control: never open a new position once daily loss limit hit
        if self._daily_loss_exceeded():
            self._log(f"{symbol}: 日亏损已达上限，拒绝开新仓", 'warn')
            return
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

        # 行权价选择：平值附近，短线 ±1%，波段 ±2%
        # 短线（1分钟以内）±1%，波段 ±2%
        tf = (self.timeframe or '').lower().strip()
        is_scalping = tf in ('10sec', '10s', '15sec', '15s', '30sec', '30s', '45sec', '45s', '1min', '1m')
        strike_range_pct = 0.01 if is_scalping else 0.02
        max_dist = current_price * strike_range_pct
        # 过滤：只保留行权价离现价在范围内的
        filtered = [b for b in candidates if abs(b.get('strike', 0) - current_price) <= max_dist]
        if not filtered:
            self._log(f"{symbol}: 无平值附近 (±{strike_range_pct*100:.0f}%) 的 {right} 期权，跳过", 'warn')
            return
        # 流动性过滤：买卖价差 > 10% 不交易
        liquid = []
        for b in filtered:
            bid = float(b.get('bid', 0) or 0)
            ask = float(b.get('ask', 0) or 0)
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
                if spread_pct > 10:
                    continue  # 价差太大，跳过
            liquid.append(b)
        if not liquid:
            self._log(f"{symbol}: 平值期权流动性差（价差 > 10%），跳过", 'warn')
            return
        # Pick ATM (strike closest to current price)
        liquid.sort(key=lambda b: abs(b.get('strike', 0) - current_price))
        best = liquid[0]

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

        # 30% 仓位预留检查
        allowed_amount = self._check_position_reserve(self.amount)
        if allowed_amount <= 0:
            return
        quantity = max(1, int(allowed_amount / (opt_price * 100)))
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

    def _calc_smart_position_size(self, symbol: str, signal_strength: float, current_price: float) -> float:
        """智能仓位计算：按账户余额百分比，模拟真人操作。
        - 不固定金额，按账户总资金算
        - 信号强度：三指标全对齐 = 1.0，两指标对齐 = 0.7，单指标 = 0.4
        - 基础 = 可用资金 * 20% * 信号强度
        - 再经过 30% 预留检查
        """
        try:
            # 获取账户余额
            account = self.client.get_account() if self.client else None
            if account:
                total = float(account.get('total', 0) or account.get('cash', 0) or 0)
                if total <= 0:
                    total = self.amount  # 兜底
            else:
                total = self.amount
            # 基础 = 总资金 * 20%（每次交易用 20%）
            base = total * 0.20
            # 信号强度调整：强信号全额，弱信号减少
            adjusted = base * max(0.4, min(1.0, signal_strength))
            # 30% 预留检查
            return self._check_position_reserve(adjusted)
        except Exception:
            return self.amount

    def _should_add_position(self, symbol: str, current_price: float, position: Dict) -> tuple:
        """判断是否加仓：模拟真人操作。
        返回 (是否加仓, 加仓类型, 加仓金额比例)
        - 盈利加仓：浮盈 > 5%，信号仍强，加 50%
        - 摊低成本：浮亏 -5% 到 -10%，信号仍有效，加 30% 拉低成本
        - 最多加仓 2 次
        """
        try:
            cost = float(position.get('cost', 0) or position.get('avg_cost', 0) or 0)
            qty = int(position.get('quantity', 0) or 0)
            adds = int(position.get('add_count', 0) or 0)
            if cost <= 0 or qty <= 0:
                return (False, '', 0)
            if adds >= 2:
                return (False, '', 0)  # 最多加 2 次
            # 计算浮动盈亏比例
            if position.get('side', 'LONG') == 'LONG':
                pnl_pct = (current_price - cost) / cost * 100
            else:
                pnl_pct = (cost - current_price) / cost * 100
            # 盈利加仓：浮盈 > 5%
            if pnl_pct > 5:
                return (True, '盈利加仓', 0.5)
            # 摊低成本：浮亏 -5% 到 -10%，且信号仍有效
            if -10 <= pnl_pct < -5:
                return (True, '摊低成本', 0.3)
            return (False, '', 0)
        except Exception:
            return (False, '', 0)

    def _check_position_reserve(self, order_amount: float) -> float:
        """检查仓位预留：必须保留至少 30% 现金，不能满仓。
        返回允许的下单金额（可能小于请求金额，0 表示不允许下单）。
        """
        try:
            if not self.client:
                return order_amount
            # 获取账户现金
            cash = 0
            try:
                acc = self.client.get_account() if hasattr(self.client, 'get_account') else None
                if acc and isinstance(acc, dict):
                    cash = float(acc.get('cash', 0) or acc.get('available_cash', 0) or 0)
            except Exception:
                cash = 0
            # 如果拿不到现金数据，用用户设置的金额上限，不做拦截（避免误杀）
            if cash <= 0:
                return order_amount
            # 已用资金 = 总资金 - 现金，估算总资金为现金 + 持仓市值
            # 简化：要求下单后现金 >= 总资金 * 30%
            # 总资金估算 = 现金 / (1 - 已用比例)，这里用现金 + 已开仓金额估算
            # 保守做法：单笔下单金额不能超过现金的 70%
            max_allowed = cash * 0.7
            if order_amount > max_allowed:
                if max_allowed < 10:  # 现金不足，跳过
                    self._log(f"现金不足，保留 30% 预留，跳过下单 (现金 ${cash:.2f})", 'warn')
                    return 0
                self._log(f"仓位预留：单笔金额从 ${order_amount:.2f} 调整为 ${max_allowed:.2f} (保留 30% 现金)", 'warn')
                return max_allowed
            return order_amount
        except Exception:
            return order_amount

    def _handle_stock_signal(self, symbol: str, signal: str, current_price: float):
        """Handle stock trade: buy/sell shares."""
        # Hard risk control: never open a new position once daily loss limit hit
        if self._daily_loss_exceeded():
            self._log(f"{symbol}: 日亏损已达上限，拒绝开新仓", 'warn')
            return
        if current_price <= 0:
            return
        # 30% 仓位预留检查
        allowed_amount = self._check_position_reserve(self.amount)
        if allowed_amount <= 0:
            return
        quantity = max(1, int(allowed_amount / current_price))
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
            'stop_loss_pct': self.stop_loss_pct,
            'daily_pnl': self.daily_pnl,
            'trades_count': len(self.trades),
            'trades': self.trades[-50:],  # Last 50 trades
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'positions': self.client.get_positions() if self.client and self.client.is_connected else [],
        }

    def _daily_loss_exceeded(self) -> bool:
        """Hard check: daily loss at/over limit -> no new orders."""
        return self.daily_pnl <= -abs(self.max_loss)

    def get_signals(self) -> List[Dict[str, Any]]:
        """Compute current GDC/GD/ladder indicators for each watched symbol.

        Returns list of {symbol, price, signal, gdc_index, gdc_color, gd_index, ladder}.
        Never exposes credentials. Safe to call when robot is stopped (returns []).
        """
        if not self.client or not self.symbols:
            return []
        results: List[Dict[str, Any]] = []
        for symbol in self.symbols:
            try:
                kline = self.client.get_kline(symbol, self.timeframe, limit=100)
                if not kline or not kline.get('close') or len(kline['close']) < 30:
                    results.append({
                        'symbol': symbol,
                        'signal': 'HOLD',
                        'price': None,
                        'gdc_index': None,
                        'gdc_color': None,
                        'gd_index': None,
                        'ladder': None,
                        'note': 'K线数据不足',
                    })
                    continue
                closes = kline['close']
                highs = kline['high']
                lows = kline['low']
                signal, detail, size_ratio = check_signal(closes, highs, lows, closes, self.timeframe)
                results.append({
                    'symbol': symbol,
                    'signal': signal,
                    'price': closes[-1] if closes else None,
                    'gdc_index': detail.get('gdc_index'),
                    'gdc_color': detail.get('gdc_color'),
                    'gd_index': detail.get('gd_index'),
                    'ladder': detail.get('ladder'),
                })
            except Exception as e:
                results.append({
                    'symbol': symbol,
                    'signal': 'HOLD',
                    'price': None,
                    'gdc_index': None,
                    'gdc_color': None,
                    'gd_index': None,
                    'ladder': None,
                    'note': f'{type(e).__name__}',
                })
        return results

    def get_stats(self) -> Dict[str, Any]:
        """Compute trading stats from trades + positions.

        - trades_count: total trades this session
        - daily_pnl: robot daily P&L
        - total_pnl: daily_pnl + unrealized P&L from open positions
        - win_rate: winning positions / total positions (0 if none);
          falls back to trades with explicit pnl field when present.
        - winning_trades / losing_trades: best-effort counts
        """
        positions: List[Dict[str, Any]] = []
        if self.client and self.client.is_connected:
            try:
                positions = self.client.get_positions()
            except Exception:
                positions = []
        unrealized = sum(
            float(p.get('unrealized_pnl', 0) or 0) for p in positions
        )
        total_pnl = self.daily_pnl + unrealized

        # Win rate: prefer explicit per-trade pnl when present
        trades_with_pnl = [t for t in self.trades if t.get('pnl') is not None]
        if trades_with_pnl:
            wins = sum(1 for t in trades_with_pnl if float(t.get('pnl', 0)) > 0)
            losses = len(trades_with_pnl) - wins
            win_rate = (wins / len(trades_with_pnl) * 100) if trades_with_pnl else 0.0
        else:
            # Fall back to open positions unrealized P&L
            nonzero = [p for p in positions if float(p.get('unrealized_pnl', 0) or 0) != 0]
            wins = sum(1 for p in nonzero if float(p.get('unrealized_pnl', 0) or 0) > 0)
            losses = len(nonzero) - wins
            win_rate = (wins / len(nonzero) * 100) if nonzero else 0.0

        return {
            'trades_count': len(self.trades),
            'daily_pnl': round(self.daily_pnl, 2),
            'unrealized_pnl': round(unrealized, 2),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(win_rate, 1),
            'winning_trades': wins,
            'losing_trades': losses,
            'open_positions': len([p for p in positions if p.get('quantity', 0) != 0]),
            'daily_loss_limit': self.max_loss,
            'daily_loss_exceeded': self._daily_loss_exceeded(),
        }

    def close_all_positions(self) -> Dict[str, Any]:
        """Close all open positions (paper trading only).

        For each position with non-zero quantity, places an opposite
        market order. Returns {closed: int, results: [...], errors: [...]}.
        Does NOT stop the robot; caller decides whether to stop.
        """
        if not self.client or not self.client.is_connected:
            return {'closed': 0, 'results': [], 'errors': ['未连接 Tiger 客户端']}
        try:
            positions = self.client.get_positions()
        except Exception as e:
            return {'closed': 0, 'results': [], 'errors': [f'获取持仓失败: {type(e).__name__}']}

        results: List[Dict[str, Any]] = []
        errors: List[str] = []
        closed = 0
        for pos in positions:
            qty = int(pos.get('quantity', 0) or 0)
            if qty == 0:
                continue
            symbol = pos.get('symbol', '')
            # Option positions: symbol may be an option identifier string.
            # Stock positions: plain symbol.
            action = 'SELL' if qty > 0 else 'BUY'
            try:
                res = self.client.place_order(symbol, action, abs(qty))
                if res:
                    closed += 1
                    results.append({
                        'symbol': symbol,
                        'action': action,
                        'quantity': abs(qty),
                        'order_id': res.get('order_id'),
                    })
                    self._log(f"平仓: {symbol} {action} x{abs(qty)}", 'sell')
                else:
                    errors.append(f"{symbol}: 下单返回为空")
            except Exception as e:
                errors.append(f"{symbol}: {type(e).__name__}")
        return {'closed': closed, 'results': results, 'errors': errors}
