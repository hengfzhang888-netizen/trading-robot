# 华尔街大拿期权波段交易机器人 - 后端

## 策略

三层过滤（GDC + GD + NX梯子）+ IV + 到期日：

1. **NX梯子（大趋势）**：价格 > EMA(H,26) = 洋红（涨）；价格 < EMA(L,26) = 绿色（跌）；中间 = 蓝色（横盘）
2. **GDC颜色（短期动能）**：价位指数上升 = 洋红；下降 = 绿色
3. **GD位置（进场点）**：价位指数 < 20 = 低位做多；> 80 = 高位做空

**信号：**
- 梯子洋红 + GDC洋红 + GD < 20 → 买 Call
- 梯子绿色 + GDC绿色 + GD > 80 → 买 Put
- 其他 → 不交易

**期权筛选：**
- IV Rank < 阈值（默认 30）才买
- 到期日 > 最短天数（默认 30 天）才买
- 行权价选平值附近（ATM）

## 安装

```bash
cd backend
pip install -r requirements.txt
```

## 运行

```bash
cd backend
python server.py
```

服务器启动在 `http://localhost:5000`。

前端网站调用 `/api/start` 等接口启动机器人。

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查 |
| `/api/start` | POST | 启动机器人（JSON 配置） |
| `/api/stop` | POST | 停止机器人 |
| `/api/status` | GET | 机器人状态、持仓、交易 |
| `/api/positions` | GET | 当前持仓 |
| `/api/trades` | GET | 交易记录 |
| `/api/logs` | GET | 日志 |
| `/api/config` | GET | 配置选项 |

### POST /api/start 请求体

```json
{
  "tigerId": "你的 Tiger ID",
  "privateKey": "你的 RSA 私钥内容（PEM）",
  "symbols": ["SPY", "NVDA"],
  "tradeType": "option",
  "timeframe": "15min",
  "amount": 1000,
  "maxPositions": 3,
  "maxLoss": 500,
  "ivThreshold": 30,
  "minDays": 30
}
```

## 安全警告（必读）

- **私钥只存在内存中**：后端收到私钥后只保存在内存变量里，绝不写文件、绝不打日志。
- **绝不分享私钥**：不要把私钥发到聊天、邮件、截图里。私钥泄露后必须去老虎开放平台重新生成。
- **只做模拟盘**：代码里强制 `sandbox=True`，实盘交易被安全检查拦截。模拟盘测试稳定后，如需实盘，必须人工审核代码并自行承担风险。
- **秒级K线限制**：Tiger API 不提供 10秒/15秒/30秒/45秒 K线，秒级周期自动回退到 1分钟。1分钟是 Tiger API 支持的最小周期。
- **日亏损上限**：达到上限后机器人自动停止，防止失控。

## 文件说明

- `tiger_client.py` - Tiger API 封装（行情、K线、期权链、下单、持仓）
- `strategy.py` - 策略计算（GDC/GD/梯子/信号/期权筛选）
- `robot.py` - 主循环（轮询、信号检查、下单、风控）
- `server.py` - Flask API 服务器（前端调用）
- `requirements.txt` - Python 依赖

## 部署到云端

后端需要常驻运行（轮询行情）。推荐部署到：

- **本地电脑**：`python server.py`，前端配置 API 地址为 `http://localhost:5000`
- **VPS/云服务器**：用 systemd 或 pm2 保持运行
- **注意**：不要把后端部署到无鉴权的公开 URL（私钥会经过网络传输，建议加 HTTPS + 鉴权）
