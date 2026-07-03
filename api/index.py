"""Vercel Python API — Flask app"""
import sys, os, json, requests as req_lib
from flask import Flask, request, jsonify, Response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

app = Flask(__name__)

@app.route('/api/scan', methods=['GET'])
def scan():
    """拉取最新数据 + 规则引擎 + 返回 HTML"""
    try:
        override = request.args.get('override', None)  # long/short/resume
        data = engine.run_engine(manual_override=override)
        return Response(data["html"], mimetype="text/html; charset=utf-8",
                       headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()[-500:]}), 500

@app.route('/api/config', methods=['GET'])
def get_config():
    cfg = engine.load_config()
    return jsonify({
        "telegram_bot_token": cfg.get("telegram_bot_token", ""),
        "telegram_chat_id": cfg.get("telegram_chat_id", ""),
        "telegram_enabled": cfg.get("telegram_enabled", False)
    })

@app.route('/api/telegram', methods=['POST', 'OPTIONS'])
def save_telegram():
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(force=True, silent=True) or {}
    cfg = engine.load_config()
    cfg["telegram_bot_token"] = data.get("bot_token", "").strip()
    cfg["telegram_chat_id"] = data.get("chat_id", "").strip()
    cfg["telegram_enabled"] = bool(data.get("enabled", False))
    try:
        with open(engine.CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Vercel 只读，需用环境变量
    return jsonify({"ok": True, "message": "Telegram 设置已保存 (注意: Vercel 环境请通过 Dashboard 设置环境变量)"})

@app.route('/api/telegram/test', methods=['GET'])
def test_telegram():
    cfg = engine.load_config()
    token = request.args.get("token", cfg.get("telegram_bot_token", ""))
    chat_id = request.args.get("chat_id", cfg.get("telegram_chat_id", ""))
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Bot Token 和 Chat ID 不能为空"})
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": "✅ Gold Trader Telegram 连接测试成功 (Vercel)", "parse_mode": "HTML"}
    try:
        r = req_lib.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            return jsonify({"ok": True, "message": "测试消息已发送，请检查你的 Telegram"})
        else:
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/api/cron', methods=['GET'])
def cron_scan():
    """Vercel Cron 触发端点：执行引擎+推送，返回 JSON 轻量响应"""
    try:
        data = engine.run_engine()
        return jsonify({
            "ok": True, 
            "push_reason": data["push_reason"],
            "in_window": data["in_window"],
            "direction": data["result"]["direction"],
            "mode": data["result"]["day_mode"],
            "signal": data["result"]["signal"]["signal"]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# Vercel Python runtime entry point
if __name__ == '__main__':
    app.run(debug=True)
