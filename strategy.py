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

def check_signal(prices, highs, lows, closes):
    """
    检查三层过滤信号
    返回: ("CALL" / "PUT" / "HOLD", 详情字典)
    """
    # 计算GDC
    gdc_index, gdc_color = calculate_gdc(prices)
    if gdc_index is None:
        return "HOLD", {"reason": "数据不足"}
    
    # 计算GD（与GDC相同）
    gd_index = gdc_index
    
    # 计算梯子
    ladder = calculate_ladder(highs, lows, closes)
    
    detail = {
        "gdc_index": round(gdc_index, 2),
        "gdc_color": gdc_color,
        "gd_index": round(gd_index, 2),
        "ladder": ladder,
    }
    
    # 三层过滤
    # 买Call：梯子洋红 + GDC洋红 + GD < 20
    if ladder == "magenta" and gdc_color == "magenta" and gd_index < 20:
        return "CALL", detail
    
    # 买Put：梯子绿色 + GDC绿色 + GD > 80
    if ladder == "green" and gdc_color == "green" and gd_index > 80:
        return "PUT", detail
    
    return "HOLD", detail

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
