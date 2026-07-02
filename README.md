# Gold Trading Terminal — Vercel Deployment

## 项目结构
```
gold-trader-vercel/
├── api/
│   ├── index.py      # Flask app (所有 API 路由)
│   ├── engine.py      # 规则引擎 + HTML 生成
│   └── config.json    # 配置
├── index.html         # 前端入口 (fetch /api/scan 获取报告)
├── requirements.txt   # Python 依赖 (flask, requests)
├── vercel.json        # Vercel 配置 (路由 + cron)
└── .gitignore
```

## 部署步骤

### 1. 安装 Vercel CLI
```bash
npm i -g vercel
```

### 2. 登录
```bash
vercel login
```

### 3. 部署
```bash
cd gold-trader-vercel
vercel --prod
```

### 4. 设置环境变量 (Telegram)
在 Vercel Dashboard → Settings → Environment Variables:
- `TELEGRAM_BOT_TOKEN` = 你的 Bot Token
- `TELEGRAM_CHAT_ID` = 1777891003
- `TELEGRAM_ENABLED` = true

### 5. Cron 配置
已内建: 工作日 UTC 7-15 时 (GMT+8 15-23) 每5分钟触发 `/api/cron`

## API 端点
- `GET /` — 前端页面
- `GET /api/scan` — 实时拉数据+生成HTML报告
- `GET /api/cron` — Cron 触发 (轻量JSON响应)
- `GET /api/config` — 读取 Telegram 配置
- `POST /api/telegram` — 保存 Telegram 设置
- `GET /api/telegram/test?token=X&chat_id=Y` — 测试 Telegram 连接

## 注意事项
- Vercel 文件系统只读，Telegram 设置需通过环境变量配置
- Cron 免费版: 每天 2 次 (Vercel 限制)，可搭配 cron-job.org 增加频率
- Serverless 冷启动: 首次访问可能慢 2-3 秒
