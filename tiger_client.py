"""
Tiger Securities API Client for 华尔街大拿 Trading Robot
Uses tigeropen SDK to connect to Tiger OpenAPI.

SECURITY:
- Credentials are passed in at runtime, NEVER hardcoded
- Private keys are used in-memory only, NEVER logged or stored to disk
- Paper trading ONLY - real account trading is blocked by safety check
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# Timeframe mapping: frontend values -> Tiger API bar periods
# Tiger API supports: 1min, 5min, 15min, 30min, 60min, day, week, month
# Sub-minute bars (10s, 15s, 30s, 45s) are NOT supported by Tiger API
# Fallback: use 1min for sub-minute requests, log a warning
TIMEFRAME_MAP = {
    '10sec': '1min',   # Fallback: Tiger API has no sub-minute bars
    '15sec': '1min',   # Fallback
    '30sec': '1min',   # Fallback
    '45sec': '1min',   # Fallback
    '1min': '1min',
    '3min': '1min',    # Tiger has no 3min, use 1min and aggregate if needed
    '5min': '5min',
    '15min': '15min',
    '60min': '60min',
}

# Poll interval in seconds for each timeframe
POLL_INTERVAL = {
    # 短线（1分钟以内）：每 2 秒检查一次，GDC 变色立刻反应，不迟疑
    '10sec': 2,
    '15sec': 2,
    '30sec': 2,
    '45sec': 2,
    '1min': 2,
    '3min': 180,
    '5min': 300,
    '15min': 900,
    '60min': 3600,
}


class TigerClient:
    """
    Wrapper around tigeropen SDK.
    All credentials passed at construction, kept in-memory only.
    """

    def __init__(self, tiger_id: str, private_key: str, account: Optional[str] = None,
                 sandbox: bool = True):
        """
        Args:
            tiger_id: Tiger ID (also used as App ID for individual developers)
            private_key: RSA private key content (PEM format, NOT a file path)
            account: Paper trading account number (optional, auto-detected if None)
            sandbox: Must be True. If False, raises error (paper trading only).
        """
        if not sandbox:
            raise ValueError(
                "SAFETY BLOCK: Real account trading is disabled. "
                "This robot only supports paper trading (sandbox=True)."
            )
        if not tiger_id or not private_key:
            raise ValueError("tiger_id and private_key are required")

        self.tiger_id = tiger_id
        # Keep private key in memory only, never log it
        self._private_key = private_key
        self.account = account
        self.sandbox = sandbox
        self._client = None
        self._connected = False

        self._init_client()

    def _init_client(self):
        """Initialize tigeropen client. Lazy import so module loads without SDK."""
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.common.consts import Language
        except ImportError:
            logger.warning(
                "tigeropen SDK not installed. Run: pip install tigeropen. "
                "Client will operate in mock mode for testing."
            )
            self._client = None
            return

        # tigeropen needs the private key as a string
        # Use sandbox (paper trading) environment
        try:
            config = TigerOpenClientConfig(
                tiger_id=self.tiger_id,
                private_key=self._private_key,
                account=self.account,
                language=Language.en_US,
                sandbox_debug=True,
            )
            self._client = config
            self._connected = True
            logger.info("Tiger client initialized (paper trading mode)")
        except Exception as e:
            # Don't include key material in error messages
            logger.error(f"Failed to initialize Tiger client: {type(e).__name__}: {e}")
            self._client = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get real-time quote for a symbol.
        Returns: {symbol, price, change_pct, volume, ...} or None on failure.
        """
        if not self._connected:
            logger.warning(f"Not connected, cannot fetch quote for {symbol}")
            return None
        # 方法1：尝试 get_stock_briefs
        try:
            from tigeropen.quote.quote_client import QuoteClient
            client = QuoteClient(self._client)
            briefs = client.get_stock_briefs([symbol])
            if briefs:
                b = briefs[0]
                price = getattr(b, 'latest_price', None)
                if price:
                    return {
                        'symbol': symbol,
                        'price': price,
                        'change_pct': getattr(b, 'change_rate', 0),
                        'volume': getattr(b, 'volume', 0),
                    }
        except Exception as e:
            logger.warning(f"get_stock_briefs failed for {symbol}: {e}, 尝试 K线 fallback")
        # 方法2：Fallback 用 K线最新收盘价
        try:
            kline = self.get_kline(symbol, period='1min', limit=2)
            if kline and kline.get('close'):
                closes = kline['close']
                price = closes[-1]
                prev = closes[-2] if len(closes) > 1 else price
                change_pct = ((price - prev) / prev * 100) if prev else 0
                return {
                    'symbol': symbol,
                    'price': price,
                    'change_pct': change_pct,
                    'volume': kline.get('volume', [0])[-1] if kline.get('volume') else 0,
                }
        except Exception as e:
            logger.error(f"get_quote K线 fallback failed for {symbol}: {e}")
        return None

    def get_kline(self, symbol: str, period: str = '15min',
                  limit: int = 100) -> Optional[Dict[str, List[float]]]:
        """
        Get K-line (candlestick) data.
        Returns: {open, high, low, close, volume, time} lists, or None on failure.
        Sub-minute periods fall back to 1min (Tiger API limitation).
        """
        api_period = TIMEFRAME_MAP.get(period, '15min')
        if period in ('10sec', '15sec', '30sec', '45sec', '3min'):
            logger.warning(
                f"Period {period} not supported by Tiger API, "
                f"using {api_period} as fallback"
            )

        if not self._connected:
            logger.warning(f"Not connected, cannot fetch K-line for {symbol}")
            return None
        try:
            from tigeropen.quote.quote_client import QuoteClient
            from tigeropen.common.consts import BarPeriod
            from tigeropen.quote.domain.filter import TimeFrame

            period_map = {
                '1min': BarPeriod.ONE_MINUTE,
                '5min': BarPeriod.FIVE_MINUTES,
                '15min': BarPeriod.FIFTEEN_MINUTES,
                '60min': BarPeriod.SIXTY_MINUTES,
            }
            bar_period = period_map.get(api_period, BarPeriod.FIFTEEN_MINUTES)

            client = QuoteClient(self._client)
            bars = client.get_bars(
                symbols=[symbol],
                period=bar_period,
                limit=limit,
            )
            if not bars:
                return None

            # bars is a DataFrame or list of bar objects
            # Normalize to dict of lists
            if hasattr(bars, 'to_dict'):
                d = bars.to_dict('list')
                return {
                    'open': d.get('open', []),
                    'high': d.get('high', []),
                    'low': d.get('low', []),
                    'close': d.get('close', []),
                    'volume': d.get('volume', []),
                    'time': d.get('time', []),
                }
            # Fallback: list of objects
            opens, highs, lows, closes, vols, times = [], [], [], [], [], []
            for b in bars:
                opens.append(getattr(b, 'open', 0))
                highs.append(getattr(b, 'high', 0))
                lows.append(getattr(b, 'low', 0))
                closes.append(getattr(b, 'close', 0))
                vols.append(getattr(b, 'volume', 0))
                times.append(getattr(b, 'time', ''))
            return {
                'open': opens, 'high': highs, 'low': lows,
                'close': closes, 'volume': vols, 'time': times,
            }
        except Exception as e:
            logger.error(f"get_kline failed for {symbol}: {type(e).__name__}: {e}")
            return None

    def get_option_chain(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get option chain for a symbol.
        Returns: {expirations: [...], calls: [...], puts: [...]} or None.
        Each option: {strike, expiry, iv, delta, price, ...}
        """
        if not self._connected:
            logger.warning(f"Not connected, cannot fetch option chain for {symbol}")
            return None
        try:
            from tigeropen.quote.quote_client import QuoteClient
            client = QuoteClient(self._client)
            # Get option expirations first
            expirations = client.get_option_expirations(symbol)
            if not expirations:
                return None
            # Get option chain for nearest expiration with >30 days
            # Simplified: return expirations, caller filters
            return {
                'symbol': symbol,
                'expirations': expirations,
            }
        except Exception as e:
            logger.error(f"get_option_chain failed for {symbol}: {type(e).__name__}: {e}")
            return None

    def get_option_briefs(self, symbol: str, expiry: str) -> Optional[List[Dict]]:
        """Get option contracts for a specific expiry."""
        if not self._connected:
            return None
        try:
            from tigeropen.quote.quote_client import QuoteClient
            client = QuoteClient(self._client)
            # tigeropen: get_option_briefs
            briefs = client.get_option_briefs(symbol, expiry)
            result = []
            for b in (briefs or []):
                result.append({
                    'identifier': getattr(b, 'identifier', ''),
                    'strike': getattr(b, 'strike', 0),
                    'expiry': getattr(b, 'expiry', expiry),
                    'right': getattr(b, 'right', ''),  # CALL / PUT
                    'iv': getattr(b, 'implied_volatility', None),
                    'price': getattr(b, 'latest_price', None),
                    'delta': getattr(b, 'delta', None),
                })
            return result
        except Exception as e:
            logger.error(f"get_option_briefs failed: {type(e).__name__}: {e}")
            return None

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions (paper trading)."""
        if not self._connected:
            return []
        try:
            from tigeropen.trade.trade_client import TradeClient
            client = TradeClient(self._client)
            positions = client.get_positions()
            result = []
            for p in (positions or []):
                result.append({
                    'symbol': getattr(p, 'contract', {}).get('symbol', '') if isinstance(getattr(p, 'contract', {}), dict) else str(getattr(p, 'contract', '')),
                    'quantity': getattr(p, 'quantity', 0),
                    'avg_cost': getattr(p, 'average_cost', 0),
                    'market_price': getattr(p, 'market_price', 0),
                    'unrealized_pnl': getattr(p, 'unrealized_pnl', 0),
                })
            return result
        except Exception as e:
            logger.error(f"get_positions failed: {type(e).__name__}: {e}")
            return []

    def get_account(self) -> Optional[Dict[str, Any]]:
        """Get account summary (paper trading)."""
        if not self._connected:
            return None
        try:
            from tigeropen.trade.trade_client import TradeClient
            client = TradeClient(self._client)
            # get_assets or similar
            assets = client.get_assets()
            if assets:
                a = assets[0] if isinstance(assets, list) else assets
                return {
                    'cash': getattr(a, 'cash', 0),
                    'net_liquidation': getattr(a, 'net_liquidation', 0),
                    'buying_power': getattr(a, 'buying_power', 0),
                }
            return None
        except Exception as e:
            logger.error(f"get_account failed: {type(e).__name__}: {e}")
            return None

    def place_order(self, symbol: str, action: str, quantity: int,
                    order_type: str = 'MKT', limit_price: Optional[float] = None,
                    account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Place an order (paper trading only).
        Args:
            symbol: Stock or option symbol (option format: e.g. 'AAPL  260117C00250000')
            action: 'BUY' or 'SELL'
            quantity: Number of shares/contracts
            order_type: 'MKT' (market) or 'LMT' (limit)
            limit_price: Required if order_type is 'LMT'
        Returns: {order_id, status} or None on failure.
        """
        if not self._connected:
            logger.error("Not connected, cannot place order")
            return None
        # Safety: double-check sandbox mode
        if not self.sandbox:
            logger.error("SAFETY BLOCK: Real trading disabled")
            return None
        try:
            from tigeropen.trade.trade_client import TradeClient
            from tigeropen.trade.domain.order import Order
            from tigeropen.common.consts import OrderType, ActionType

            client = TradeClient(self._client)
            order = Order(
                account=account or self.account,
                contract=symbol,
                action=ActionType.BUY if action.upper() == 'BUY' else ActionType.SELL,
                order_type=OrderType.MARKET if order_type == 'MKT' else OrderType.LIMIT,
                quantity=quantity,
            )
            if order_type == 'LMT' and limit_price is not None:
                order.limit_price = limit_price

            result = client.place_order(order)
            logger.info(f"Order placed: {symbol} {action} x{quantity}, result={result}")
            return {
                'order_id': getattr(result, 'order_id', str(result)),
                'status': 'submitted',
                'symbol': symbol,
                'action': action,
                'quantity': quantity,
            }
        except Exception as e:
            logger.error(f"place_order failed for {symbol}: {type(e).__name__}: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not self._connected:
            return False
        try:
            from tigeropen.trade.trade_client import TradeClient
            client = TradeClient(self._client)
            client.cancel_order(order_id)
            return True
        except Exception as e:
            logger.error(f"cancel_order failed: {type(e).__name__}: {e}")
            return False


def get_poll_interval(timeframe: str) -> int:
    """Get poll interval in seconds for a timeframe."""
    return POLL_INTERVAL.get(timeframe, 60)
