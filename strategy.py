"""
华尔街大拿期权波段交易机器人 - 策略模块
实现 GDC + GD + NX梯子 三层过滤策略
"""
import math

def calculate_gdc(prices, n=30, m1=10, m2=5):
    """
    计算GDC指标的价位指数
    返回: 价位指数列表，最后一个值是当前值
    """
    if len(prices) < n:
        return None, None
    
    # RSV
    rsv_list = []
    for i in range(n-1, len(prices)):
        window = prices[i-n+1:i+1]
        low_min = min(window)
        high_max = max(window)
        if high_max == low_min:
            rsv = 50
        else:
            rsv = (prices[i] - low_min) / (high_max - low_min) * 100
        rsv_list.append(rsv)
    
    # SMA
    def sma(data, period):
        result = []
        for i in range(len(data)):
            if i < period - 1:
                result.append(data[i])
            else:
                window = data[i-period+1:i+1]
                result.append(sum(window) / period)
        return result
    
    mean1 = sma(rsv_list, m1)
    mean2 = sma(mean1, m2)
    
    # EMA(均值2, 3)
    ema_list = []
    alpha = 2 / (3 + 1)
    for i, val in enumerate(mean2):
        if i == 0:
            ema_list.append(val)
        else:
            ema_list.append(alpha * val + (1 - alpha) * ema_list[-1])
    
    current_index = ema_list[-1] if ema_list else 50
    prev_index = ema_list[-2] if len(ema_list) >= 2 else current_index
    
    # GDC颜色：洋红=上涨，绿色=下跌
    gdc_color = "magenta" if current_index >= prev_index else "green"
    
    return current_index, gdc_color

def calculate_gd(prices, n=30, m1=10, m2=5):
    """
    计算GD指标的价位指数
    与GDC相同逻辑，但用于判断80/20位置
    """
    return calculate_gdc(prices, n, m1, m2)

def calculate_ladder(highs, lows, closes, n=26):
    """
    计算NX梯子趋势
    返回: "magenta" (上涨) / "blue" (横盘) / "green" (下跌)
    """
    if len(closes) < n:
        return "blue"
    
    # EMA(H, 26) 和 EMA(L, 26)
    def ema(data, period):
        result = []
        alpha = 2 / (period + 1)
        for i, val in enumerate(data):
            if i == 0:
                result.append(val)
            else:
                result.append(alpha * val + (1 - alpha) * result[-1])
        return result
    
    ema_h = ema(highs, n)
    ema_l = ema(lows, n)
    
    current_close = closes[-1]
    upper = ema_h[-1]
    lower = ema_l[-1]
    
    if current_close > upper:
        return "magenta"  # 大趋势向上
    elif current_close < lower:
        return "green"  # 大趋势向下
    else:
        return "blue"  # 横盘震荡

def _is_scalping_timeframe(timeframe: str) -> bool:
    """判断是否为 1 分钟以内的短线（10/15/30/45秒、1分钟）"""
    tf = (timeframe or '').lower().strip()
    return tf in ('10sec', '10s', '15sec', '15s', '30sec', '30s', '45sec', '45s', '1min', '1m')

def _get_gdc_trend(prices):
    """计算 GDC 趋势：上升 / 下降 / 平（用于短线判断）
    返回: (gdc_index, gdc_color, trend, trend_strength)
    trend: 'up' / 'down' / 'flat'
    trend_strength: 0.0-1.0，变化幅度越大越强
    """
    gdc_index, gdc_color = calculate_gdc(prices)
    if gdc_index is None:
        return None, None, 'flat', 0.0
    # 计算 GDC 的变化：用最近 3 个点的斜率
    # 这里简化：用当前值与前值比较，配合颜色判断
    # 实际实现中，calculate_gdc 返回的颜色已包含涨跌信息
    # trend_strength 用 GDC 偏离 50 的程度估算
    if gdc_color == 'magenta':
        trend = 'up'
        # GDC 越接近 80 越强，越接近 50 越弱
        strength = min(1.0, abs(gdc_index - 50) / 30.0)
    else:
        trend = 'down'
        strength = min(1.0, abs(gdc_index - 50) / 30.0)
    # 如果 GDC 在 45-55 之间，视为平（无动能），不交易
    if 45 <= gdc_index <= 55 and strength < 0.2:
        trend = 'flat'
        strength = 0.0
    return gdc_index, gdc_color, trend, strength

def check_signal(prices, highs, lows, closes, timeframe='15min'):
    """
    检查信号（双模式）
    - 1分钟以内短线：GDC 为主，梯子颜色为辅（GDC 趋势决定方向，梯子颜色决定仓位）
    - 1分钟以上波段：三层过滤全一致才交易（严格模式）
    返回: ("CALL" / "PUT" / "HOLD", 详情字典，含 position_size_ratio)
    """
    # 计算GDC
    gdc_index, gdc_color = calculate_gdc(prices)
    if gdc_index is None:
        return "HOLD", {"reason": "数据不足"}, 0.0

    # 计算GD（与GDC相同）
    gd_index = gdc_index

    # 计算梯子（颜色）
    ladder = calculate_ladder(highs, lows, closes)

    detail = {
        "gdc_index": round(gdc_index, 2),
        "gdc_color": gdc_color,
        "gd_index": round(gd_index, 2),
        "ladder": ladder,
        "timeframe": timeframe,
    }

    # === 短线模式：1分钟以内（10/15/30/45秒、1分钟）===
    if _is_scalping_timeframe(timeframe):
        gdc_idx, _, gdc_trend, _ = _get_gdc_trend(prices)
        detail['mode'] = 'scalping'
        detail['gdc_trend'] = gdc_trend

        # 风险规避：GDC 平（无动能）不交易
        if gdc_trend == 'flat':
            detail['reason'] = 'GDC 平，无动能，避开'
            return "HOLD", detail, 0.0

        # GDC 上升 → 买 Call
        if gdc_trend == 'up':
            # 梯子洋红（顺势）= 全仓
            if ladder == 'magenta':
                detail['reason'] = 'GDC 上升 + 梯子洋红，顺势买 Call'
                return "CALL", detail, 1.0
            # 梯子绿色（逆势抢反弹）= 半仓，快进快出
            if ladder == 'green':
                detail['reason'] = 'GDC 上升 + 梯子绿色，逆势抢反弹买 Call（半仓）'
                return "CALL", detail, 0.5
            # 梯子蓝色（横盘）= 小仓
            detail['reason'] = 'GDC 上升 + 梯子蓝色，横盘反弹买 Call（小仓）'
            return "CALL", detail, 0.3

        # GDC 下降 → 买 Put
        if gdc_trend == 'down':
            # 梯子绿色（顺势）= 全仓
            if ladder == 'green':
                detail['reason'] = 'GDC 下降 + 梯子绿色，顺势买 Put'
                return "PUT", detail, 1.0
            # 梯子洋红（逆势抢回调）= 半仓
            if ladder == 'magenta':
                detail['reason'] = 'GDC 下降 + 梯子洋红，逆势抢回调买 Put（半仓）'
                return "PUT", detail, 0.5
            # 梯子蓝色 = 小仓
            detail['reason'] = 'GDC 下降 + 梯子蓝色，横盘回调买 Put（小仓）'
            return "PUT", detail, 0.3

        return "HOLD", detail, 0.0

    # === 波段模式：1分钟以上（严格三层过滤）===
    detail['mode'] = 'swing'
    # 买Call：梯子洋红 + GDC洋红 + GD < 20
    if ladder == "magenta" and gdc_color == "magenta" and gd_index < 20:
        detail['reason'] = '三层全一致，买 Call'
        return "CALL", detail, 1.0

    # 买Put：梯子绿色 + GDC绿色 + GD > 80
    if ladder == "green" and gdc_color == "green" and gd_index > 80:
        detail['reason'] = '三层全一致，买 Put'
        return "PUT", detail, 1.0

    detail['reason'] = '三层不一致，不交易'
    return "HOLD", detail, 0.0

def check_option_filter(iv_rank, days_to_expiry, iv_threshold=30, min_days=30):
    """
    检查期权筛选条件
    返回: (是否通过, 原因)
    """
    if iv_rank is None:
        return False, "IV Rank未知"
    
    if iv_rank >= iv_threshold:
        return False, f"IV Rank {iv_rank} >= {iv_threshold}，期权太贵"
    
    if days_to_expiry < min_days:
        return False, f"到期日 {days_to_expiry}天 < {min_days}天，Theta衰减太快"
    
    return True, "通过"
