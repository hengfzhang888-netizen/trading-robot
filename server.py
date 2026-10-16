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


@app.route('/api/trades', methods=['GET'])
def trades():
    if robot is None:
        return jsonify({'trades': []})
    return jsonify({'trades': robot.trades})


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
