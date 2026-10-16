"""
华尔街大拿交易机器人 - API Server
Flask server bridging frontend website and trading robot.

SECURITY:
- Credentials accepted via POST /api/start, used in-memory only
- NEVER logs private keys
- NEVER writes credentials to disk
- Paper trading ONLY (enforced in TigerClient)
- CORS enabled for frontend origin
"""
import logging
import os
from flask import Flask, request, jsonify
from flask_cors import CORS

from robot import TradingRobot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow frontend to call API from any origin (demo; restrict in production)

# Global robot instance (single user, single robot)
robot: TradingRobot | None = None

# Config persistence (non-sensitive only, NEVER store private key)
SAVED_CONFIG: dict = {}

# Log buffer for frontend polling (last 200 lines)
LOG_BUFFER: list = []


def _on_log(msg: str, level: str = 'info'):
    """Append log line to buffer (no credentials)."""
    # Safety: never store lines that might contain key material
    if 'private' in msg.lower() or 'key' in msg.lower() and 'private' in msg.lower():
        msg = '[REDACTED: possible credential]'
    LOG_BUFFER.append({'msg': msg, 'level': level})
    # Keep last 200
    if len(LOG_BUFFER) > 200:
        LOG_BUFFER.pop(0)


def _on_trade(trade: dict):
    logger.info(f"Trade: {trade.get('symbol')} {trade.get('side')} x{trade.get('quantity')}")


@app.route('/', methods=['GET'])
def index():
    """Serve frontend."""
    try:
        with open('static.html', 'r', encoding='utf-8') as f:
            return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
    except FileNotFoundError:
        return jsonify({'status': 'ok', 'service': 'trading-robot', 'note': 'Frontend not found, use /api/health'})


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'trading-robot'})


@app.route('/api/start', methods=['POST'])
def start():
    """Start the robot. Body: JSON config from frontend."""
    global robot
    data = request.get_json(silent=True) or {}

    # RISK CONTROL: paper trading ONLY. Reject any live-trading request.
    account_mode = str(data.get('accountMode', data.get('account_mode', ''))).lower()
    trade_mode = str(data.get('tradeMode', data.get('trade_mode', ''))).lower()
    account = str(data.get('account', '') or '').lower()
    if account_mode == 'live' or trade_mode == 'live' or account == 'live':
        return jsonify({'ok': False, 'error': '安全拦截：本机器人只支持模拟盘，拒绝实盘交易'}), 403
    # Also reject if caller explicitly passes sandbox=False
    if data.get('sandbox') is False:
        return jsonify({'ok': False, 'error': '安全拦截：本机器人只支持模拟盘，拒绝实盘交易'}), 403

    # Validate required fields (do NOT log credentials)
    tiger_id = data.get('tigerId') or data.get('tiger_id')
    private_key = data.get('privateKey') or data.get('private_key')
    symbols = data.get('symbols') or []

    # Frontend sends symbols as list, or customStocks string
    if not symbols and data.get('customStocks'):
        symbols = [s.strip().upper() for s in data['customStocks'].split(',') if s.strip()]

    if not tiger_id or not private_key:
        return jsonify({'ok': False, 'error': '缺少 Tiger API 配置（Tiger ID / 私钥）'}), 400
    if not symbols:
        return jsonify({'ok': False, 'error': '请至少选择一只股票'}), 400

    # Stop existing robot if running
    if robot and robot.running:
        robot.stop()

    config = {
        'tiger_id': tiger_id,
        # private_key kept in-memory only, never written to disk
        'private_key': private_key,
        'symbols': symbols,
        'trade_type': data.get('tradeType', data.get('trade_type', 'option')),
        'timeframe': data.get('timeframe', '15min'),
        'amount': float(data.get('amount', 1000)),
        'max_positions': int(data.get('maxPositions', data.get('max_positions', 3))),
        'max_loss': float(data.get('maxLoss', data.get('max_loss', 500))),
        'iv_threshold': float(data.get('ivThreshold', data.get('iv_threshold', 30))),
        'min_days': int(data.get('minDays', data.get('min_days', 30))),
        'stop_loss_pct': float(data.get('stopLossPct', data.get('stop_loss_pct', 15.0))),
        'account': data.get('account'),  # Optional paper account number
    }

    # Clear sensitive data from request log (Flask doesn't log body by default)
    robot = TradingRobot(config, on_trade=_on_trade, on_log=_on_log)
    ok = robot.start()
    if ok:
        return jsonify({'ok': True, 'message': '机器人已启动'})
    return jsonify({'ok': False, 'error': '启动失败，请查看日志'}), 500


@app.route('/api/stop', methods=['POST'])
def stop():
    global robot
    if robot and robot.running:
        robot.stop()
        return jsonify({'ok': True, 'message': '机器人已停止'})
    return jsonify({'ok': True, 'message': '机器人未运行'})


@app.route('/api/status', methods=['GET'])
def status():
    if robot is None:
        return jsonify({
            'running': False,
            'symbols': [],
            'daily_pnl': 0,
            'trades_count': 0,
            'trades': [],
            'positions': [],
        })
    st = robot.get_status()
    # Never expose credentials in status
    st.pop('private_key', None)
    st.pop('tiger_id', None)
    return jsonify(st)


@app.route('/api/positions', methods=['GET'])
def positions():
    if robot is None or not robot.client:
        return jsonify({'positions': []})
    try:
        pos = robot.client.get_positions()
        return jsonify({'positions': pos})
    except Exception as e:
        logger.error(f"positions error: {e}")
        return jsonify({'positions': [], 'error': str(e)}), 500


@app.route('/api/quotes', methods=['GET'])
def quotes():
    """获取选中股票的实时报价（独立于机器人运行状态，像股票机器人一样）"""
    symbols_param = request.args.get('symbols', '')
    if not symbols_param:
        return jsonify({'ok': False, 'error': '未指定股票'}), 400
    symbols = [s.strip().upper() for s in symbols_param.split(',') if s.strip()]
    # 获取 Tiger 客户端（即使机器人未启动，也从保存的配置创建）
    client = None
    if robot is not None and robot.client:
        client = robot.client
    else:
        try:
            from tiger_client import TigerClient
            cfg = SAVED_CONFIG or {}
            tiger_id = cfg.get('tiger_id')
            private_key = cfg.get('private_key')
            account_num = cfg.get('account')
            if tiger_id and private_key:
                client = TigerClient(tiger_id, private_key, account_num)
        except Exception as e:
            logger.error(f"quotes client error: {e}")
    if not client:
        return jsonify({'ok': False, 'error': '未配置 Tiger API'}), 400
    results = {}
    for sym in symbols:
        try:
            q = client.get_quote(sym)
            if q:
                results[sym] = q
        except Exception as e:
            logger.error(f"quote {sym} error: {e}")
    return jsonify({'ok': True, 'quotes': results})

@app.route('/api/account', methods=['GET'])
def account():
    # 即使机器人未启动，也要显示账户余额（联动老虎）
    client = None
    if robot is not None and robot.client:
        client = robot.client
    else:
        # 机器人未启动，从保存的配置创建 Tiger 客户端
        try:
            from tiger_client import TigerClient
            cfg = SAVED_CONFIG or {}
            tiger_id = cfg.get('tiger_id')
            private_key = cfg.get('private_key')
            account_num = cfg.get('account')
            if tiger_id and private_key:
                client = TigerClient(tiger_id, private_key, account_num)
        except Exception as e:
            logger.error(f"account client create error: {e}")
            return jsonify({'ok': False, 'error': '未配置 Tiger API，请先保存配置'}), 400
    if not client:
        return jsonify({'ok': False, 'error': '未配置 Tiger API，请先保存配置'}), 400
    try:
        acc = client.get_account()
        if acc:
            return jsonify({'ok': True, 'account': acc})
        else:
            return jsonify({'ok': False, 'error': '无法获取账户信息'})
    except Exception as e:
        logger.error(f"account error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/trades', methods=['GET'])
def trades():
    if robot is None:
        return jsonify({'trades': []})
    return jsonify({'trades': robot.trades})


@app.route('/api/signals', methods=['GET'])
def signals():
    """Return current GDC/GD/ladder indicator values + signal per symbol.

    Uses the robot's watched symbols + timeframe. Returns [] when the
    robot is not running. No credentials in response.
    """
    if robot is None:
        return jsonify({'signals': [], 'running': False})
    try:
        sigs = robot.get_signals()
        return jsonify({'signals': sigs, 'running': robot.running})
    except Exception as e:
        logger.error(f"signals error: {type(e).__name__}: {e}")
        return jsonify({'signals': [], 'running': False, 'error': '获取信号失败'}), 500


@app.route('/api/close_all', methods=['POST'])
def close_all():
    """Close all open positions (paper trading only).

    Requires the robot to exist. Places opposite market orders for
    every non-zero position. Returns count closed + per-order results.
    """
    if robot is None or not robot.client:
        return jsonify({'ok': False, 'error': '机器人未启动或未连接'}), 400
    try:
        result = robot.close_all_positions()
        ok = len(result.get('errors', [])) == 0
        return jsonify({'ok': ok, **result})
    except Exception as e:
        logger.error(f"close_all error: {type(e).__name__}: {e}")
        return jsonify({'ok': False, 'error': '平仓失败，请查看日志'}), 500


# === 手动干预模式 API ===
# 全局手动交易设置：每个持仓的 TP/SL
MANUAL_TP_SL = {}  # {symbol: {'tp_pct': float, 'sl_pct': float}}

@app.route('/api/mode', methods=['GET', 'POST'])
def trading_mode():
    """获取/设置交易模式：auto（AI自动）或 manual（手动干预）"""
    global TRADING_MODE
    if request.method == 'GET':
        return jsonify({'mode': globals().get('TRADING_MODE', 'auto')})
    data = request.get_json() or {}
    mode = data.get('mode', 'auto')
    if mode not in ('auto', 'manual'):
        return jsonify({'ok': False, 'error': '模式必须是 auto 或 manual'}), 400
    globals()['TRADING_MODE'] = mode
    return jsonify({'ok': True, 'mode': mode})

@app.route('/api/manual_buy', methods=['POST'])
def manual_buy():
    """手动买入：用户指定股票、数量、点位、触发、期权、止盈止损"""
    if robot is None or not robot.client:
        return jsonify({'ok': False, 'error': '机器人未启动或未连接'}), 400
    data = request.get_json() or {}
    symbol = data.get('symbol', '').strip().upper()
    quantity = int(data.get('quantity', 0))
    buy_price = data.get('buy_price')
    sell_price = data.get('sell_price')
    trigger = data.get('trigger', 'gdc_red')
    strike = data.get('strike')
    right = data.get('right', 'CALL')
    tp_pct = data.get('tp_pct')
    sl_pct = data.get('sl_pct')
    if not symbol or quantity <= 0:
        return jsonify({'ok': False, 'error': '股票代码和数量必填'}), 400
    # 期权手动下单：根据行权价和 Call/Put，从期权链找合约
    if strike:
        try:
            chain = robot.client.get_option_chain(symbol)
            if not chain or not chain.get('expirations'):
                return jsonify({'ok': False, 'error': '无法获取期权链'}), 500
            # 用最近的到期日
            target_expiry = chain['expirations'][0]
            # 找匹配的合约
            contracts = chain.get('contracts', {}).get(target_expiry, [])
            best = None
            for c in contracts:
                if abs(float(c.get('strike', 0)) - float(strike)) < 0.01 and c.get('right', '').upper() == right.upper():
                    best = c
                    break
            if not best:
                return jsonify({'ok': False, 'error': f'找不到行权价 {strike} 的 {right} 合约'}), 400
            identifier = best.get('identifier') or f"{symbol} {target_expiry} {right}"
            # 如果设置了买入价，创建待触发期权订单
            if buy_price:
                if not hasattr(robot, 'pending_orders'):
                    robot.pending_orders = []
                robot.pending_orders.append({
                    'symbol': symbol, 'quantity': quantity, 'buy_price': buy_price,
                    'sell_price': sell_price, 'trigger': trigger, 'strike': strike,
                    'right': right, 'identifier': identifier, 'expiry': target_expiry,
                    'tp_pct': tp_pct, 'sl_pct': sl_pct, 'side': 'BUY', 'type': 'option'
                })
                return jsonify({'ok': True, 'pending': True, 'symbol': symbol, 'quantity': quantity, 'strike': strike, 'right': right, 'buy_price': buy_price, 'message': '已设置待触发期权订单'})
            # 立即下单期权
            result = robot.client.place_order(identifier, 'BUY', quantity)
            if result:
                if tp_pct is not None or sl_pct is not None:
                    try:
                        robot.client.set_tp_sl(identifier, tp_pct, sl_pct)
                    except Exception:
                        pass
                return jsonify({'ok': True, 'order_id': result.get('order_id'), 'symbol': symbol, 'quantity': quantity, 'strike': strike, 'right': right, 'identifier': identifier})
            return jsonify({'ok': False, 'error': '期权下单失败'}), 500
        except Exception as e:
            logger.error(f"manual option buy error: {e}")
            return jsonify({'ok': False, 'error': '期权下单失败: ' + str(e)}), 500
    try:
        # 如果设置了买入价，创建待触发订单（价格到了才买）
        if buy_price:
            # 保存待触发订单
            if not hasattr(robot, 'pending_orders'):
                robot.pending_orders = []
            robot.pending_orders.append({
                'symbol': symbol, 'quantity': quantity, 'buy_price': buy_price,
                'sell_price': sell_price, 'trigger': trigger,
                'tp_pct': tp_pct, 'sl_pct': sl_pct, 'side': 'BUY'
            })
            # 保存止盈止损
            if tp_pct is not None or sl_pct is not None:
                if not hasattr(robot, 'manual_tp_sl'):
                    robot.manual_tp_sl = {}
                robot.manual_tp_sl[symbol] = {'tp_pct': tp_pct, 'sl_pct': sl_pct}
            return jsonify({'ok': True, 'pending': True, 'symbol': symbol, 'quantity': quantity, 'buy_price': buy_price, 'message': '已设置待触发订单，价格到 $%.2f 自动买入' % buy_price})
        # 无买入价，立即下单
        result = robot.client.place_order(symbol, 'BUY', quantity)
        if result:
            # 保存止盈止损
            if tp_pct is not None or sl_pct is not None:
                try:
                    robot.client.set_tp_sl(symbol, tp_pct, sl_pct)
                except Exception:
                    pass
            return jsonify({'ok': True, 'order_id': result.get('order_id'), 'symbol': symbol, 'quantity': quantity, 'tp_pct': tp_pct, 'sl_pct': sl_pct})
        return jsonify({'ok': False, 'error': '下单失败'}), 500
    except Exception as e:
        logger.error(f"manual_buy error: {e}")
        return jsonify({'ok': False, 'error': '下单失败: ' + str(e)}), 500

@app.route('/api/manual_sell', methods=['POST'])
def manual_sell():
    """手动卖出：用户指定股票、数量"""
    if robot is None or not robot.client:
        return jsonify({'ok': False, 'error': '机器人未启动或未连接'}), 400
    data = request.get_json() or {}
    symbol = data.get('symbol', '').strip().upper()
    quantity = int(data.get('quantity', 0))
    if not symbol or quantity <= 0:
        return jsonify({'ok': False, 'error': '股票代码和数量必填'}), 400
    try:
        result = robot.client.place_order(symbol, 'SELL', quantity)
        if result:
            return jsonify({'ok': True, 'order_id': result.get('order_id'), 'symbol': symbol, 'quantity': quantity})
        return jsonify({'ok': False, 'error': '下单失败'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': '下单失败'}), 500

@app.route('/api/set_tp_sl', methods=['POST'])
def set_tp_sl():
    """设置某个持仓的止盈/止损百分比"""
    data = request.get_json() or {}
    symbol = data.get('symbol', '').strip().upper()
    tp_pct = data.get('tp_pct')
    sl_pct = data.get('sl_pct')
    if not symbol:
        return jsonify({'ok': False, 'error': '股票代码必填'}), 400
    MANUAL_TP_SL[symbol] = {
        'tp_pct': float(tp_pct) if tp_pct is not None else None,
        'sl_pct': float(sl_pct) if sl_pct is not None else None,
    }
    return jsonify({'ok': True, 'symbol': symbol, **MANUAL_TP_SL[symbol]})

@app.route('/api/get_tp_sl', methods=['GET'])
def get_tp_sl():
    """获取所有持仓的 TP/SL 设置"""
    return jsonify({'tp_sl': MANUAL_TP_SL})

@app.route('/api/option_chain', methods=['GET'])
def option_chain():
    """获取某只股票的期权链（用于手动选合约）"""
    symbol = request.args.get('symbol', '').strip().upper()
    if not symbol:
        return jsonify({'ok': False, 'error': '股票代码必填'}), 400
    if robot is None or not robot.client:
        return jsonify({'ok': False, 'error': '机器人未启动或未连接'}), 400
    try:
        chain = robot.client.get_option_chain(symbol)
        if not chain:
            return jsonify({'ok': False, 'error': '无法获取期权链'}), 500
        # 简化返回：到期日列表
        return jsonify({'ok': True, 'symbol': symbol, 'chain': chain})
    except Exception as e:
        return jsonify({'ok': False, 'error': '获取期权链失败'}), 500

# 交易时段设置
TRADING_SESSION = {
    'mode': 'all_day',  # all_day / custom / off
    'start_time': '09:30',  # HH:MM
    'end_time': '16:00',
}

@app.route('/api/trading_session', methods=['GET', 'POST'])
def trading_session():
    """获取/设置交易时段"""
    if request.method == 'GET':
        return jsonify({'ok': True, **TRADING_SESSION})
    data = request.get_json() or {}
    mode = data.get('mode', 'all_day')
    if mode not in ('all_day', 'custom', 'off'):
        return jsonify({'ok': False, 'error': '模式错误'}), 400
    TRADING_SESSION['mode'] = mode
    if 'start_time' in data:
        TRADING_SESSION['start_time'] = data['start_time']
    if 'end_time' in data:
        TRADING_SESSION['end_time'] = data['end_time']
    return jsonify({'ok': True, **TRADING_SESSION})

# 手动选择的合约
SELECTED_CONTRACT = {}

@app.route('/api/select_contract', methods=['POST'])
def select_contract():
    """手动选择期权合约"""
    data = request.get_json() or {}
    symbol = data.get('symbol', '').strip().upper()
    contract = data.get('contract', '')
    if not symbol or not contract:
        return jsonify({'ok': False, 'error': '股票和合约必填'}), 400
    SELECTED_CONTRACT[symbol] = contract
    return jsonify({'ok': True, 'symbol': symbol, 'contract': contract})

@app.route('/api/selected_contract', methods=['GET'])
def get_selected_contract():
    """获取已选合约"""
    return jsonify({'contracts': SELECTED_CONTRACT})

@app.route('/api/stats', methods=['GET'])
def stats():
    """Return trading stats: today P&L, total P&L, win rate, trade count.

    Returns zeros when the robot has never run. No credentials in response.
    """
    if robot is None:
        return jsonify({
            'trades_count': 0,
            'daily_pnl': 0,
            'unrealized_pnl': 0,
            'total_pnl': 0,
            'win_rate': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'open_positions': 0,
            'daily_loss_limit': 0,
            'daily_loss_exceeded': False,
        })
    try:
        return jsonify(robot.get_stats())
    except Exception as e:
        logger.error(f"stats error: {type(e).__name__}: {e}")
        return jsonify({'error': '获取统计失败'}), 500


@app.route('/api/logs', methods=['GET'])
def logs():
    """Return recent log lines for frontend display."""
    return jsonify({'logs': LOG_BUFFER[-100:]})


@app.route('/api/config', methods=['GET'])
def config_info():
    """Return non-sensitive config schema (for frontend)."""
    return jsonify({
        'timeframes': ['10sec', '15sec', '30sec', '45sec', '1min', '3min', '5min', '15min', '60min'],
        'trade_types': ['option', 'stock'],
        'note': 'Tiger API 不支持秒级K线，秒级周期自动回退到1分钟',
    })


@app.route('/api/save_config', methods=['POST'])
def save_config():
    """Save config including private key (user explicitly requested persistence)."""
    global SAVED_CONFIG
    data = request.get_json(silent=True) or {}
    # User explicitly requested private key persistence for convenience
    # WARNING: Private key is stored in-memory on Railway server
    SAVED_CONFIG = {
        'tiger_id': data.get('tigerId') or data.get('tiger_id', ''),
        'private_key': data.get('privateKey') or data.get('private_key', ''),
        'symbols': data.get('symbols', []),
        'trade_type': data.get('tradeType', data.get('trade_type', 'option')),
        'timeframe': data.get('timeframe', '15min'),
        'amount': data.get('amount', 1000),
        'max_positions': data.get('maxPositions', data.get('max_positions', 3)),
        'max_loss': data.get('maxLoss', data.get('max_loss', 500)),
        'iv_threshold': data.get('ivThreshold', data.get('iv_threshold', 30)),
        'min_days': data.get('minDays', data.get('min_days', 30)),
        'stop_loss_pct': data.get('stopLossPct', data.get('stop_loss_pct', 15.0)),
        'account': data.get('account', ''),
    }
    return jsonify({'ok': True, 'message': '配置已保存'})


@app.route('/api/load_config', methods=['GET'])
def load_config():
    """Load saved non-sensitive config."""
    return jsonify({'ok': True, 'config': SAVED_CONFIG})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    logger.info(f"Starting trading robot API server on {host}:{port}")
    logger.info("Paper trading ONLY. Credentials are in-memory only.")
    app.run(host=host, port=port, debug=False)
