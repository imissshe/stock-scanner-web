#!/usr/bin/env python3
"""股票异动扫描器网页版：Flask 包装 scan.py，对外提供扫描页面与 API。"""
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import scan as scanner

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESS_STARTED_AT = time.time()

# ---------------- 控制面板（/panel）----------------
# 密码哈希保存在 panel_auth.json（不入库、不进代码）；首次未设置时 /panel 进入设置模式。
AUTH_FILE = os.path.join(BASE_DIR, "panel_auth.json")


def _load_auth():
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_auth(data):
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, AUTH_FILE)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except Exception:
        pass


_auth = _load_auth()
if not _auth.get("secret_key"):
    _auth["secret_key"] = secrets.token_hex(32)
    _save_auth(_auth)
app.secret_key = _auth["secret_key"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, PERMANENT_SESSION_LIFETIME=86400 * 7)

_login_fails = {}  # ip -> [fail_count, locked_until]


def _panel_ready():
    return bool(_load_auth().get("password_hash"))


def _login_locked(ip):
    rec = _login_fails.get(ip)
    return bool(rec and rec[1] > time.time())


def _note_login_fail(ip):
    rec = _login_fails.get(ip, [0, 0])
    rec[0] += 1
    if rec[0] >= 5:
        rec[1] = time.time() + 300
        rec[0] = 0
    _login_fails[ip] = rec


def panel_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("panel_authed"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)

    return wrapper


@app.route("/panel")
def panel_page():
    return render_template("panel.html", ready=_panel_ready(),
                           authed=bool(session.get("panel_authed")))


@app.route("/panel/setup", methods=["POST"])
def panel_setup():
    if _panel_ready():
        return jsonify({"error": "already_set"}), 400
    pw = (request.get_json(force=True, silent=True) or {}).get("password") or ""
    if len(pw) < 8:
        return jsonify({"error": "密码至少 8 位"}), 400
    auth = _load_auth()
    auth["password_hash"] = generate_password_hash(pw)
    _save_auth(auth)
    session["panel_authed"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.route("/panel/login", methods=["POST"])
def panel_login():
    ip = request.remote_addr or "?"
    if _login_locked(ip):
        return jsonify({"error": "尝试次数过多，锁定 5 分钟"}), 429
    pw = (request.get_json(force=True, silent=True) or {}).get("password") or ""
    auth = _load_auth()
    if auth.get("password_hash") and check_password_hash(auth["password_hash"], pw):
        session["panel_authed"] = True
        session.permanent = True
        _login_fails.pop(ip, None)
        return jsonify({"ok": True})
    _note_login_fail(ip)
    return jsonify({"error": "密码不对"}), 403


@app.route("/panel/logout", methods=["POST"])
def panel_logout():
    session.pop("panel_authed", None)
    return jsonify({"ok": True})


@app.route("/panel/api/status")
@panel_required
def panel_status():
    mem = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0][:-1]] = int(parts[1]) // 1024  # MB
    except Exception:
        pass
    disk = shutil.disk_usage(BASE_DIR)
    try:
        load = os.getloadavg()
    except Exception:
        load = (0, 0, 0)
    return jsonify({
        "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "process_uptime_sec": int(time.time() - PROCESS_STARTED_AT),
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "cwd": BASE_DIR,
        "home": os.path.expanduser("~"),
        "hostname": os.uname().nodename,
        "load_1m": round(load[0], 2),
        "mem_total_mb": mem.get("MemTotal"),
        "mem_available_mb": mem.get("MemAvailable"),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "disk_free_gb": round(disk.free / 1e9, 1),
    })


@app.route("/panel/api/logs")
@panel_required
def panel_logs():
    try:
        lines = min(int(request.args.get("lines") or 200), 1000)
    except Exception:
        lines = 200
    out = {}
    for name in ("nohup.out", "app.log", "scanner.log"):
        path = os.path.join(BASE_DIR, name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    out[name] = "".join(f.readlines()[-lines:])[-20000:]
            except Exception as e:  # noqa: BLE001
                out[name] = f"(读取失败: {e})"
    if not out:
        out["(无日志文件)"] = "目录下暂无 nohup.out / app.log / scanner.log"
    return jsonify(out)


@app.route("/panel/api/restart", methods=["POST"])
@panel_required
def panel_restart():
    def _reexec():
        time.sleep(0.6)
        # exec 前关掉继承的监听 socket，否则新进程会撞上 "Address already in use"
        try:
            for fd in os.listdir("/proc/self/fd"):
                try:
                    n = int(fd)
                    if n > 2:
                        os.close(n)
                except Exception:
                    pass
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_reexec, daemon=True).start()
    return jsonify({"ok": True, "msg": "正在重启，约 3 秒后刷新页面"})


@app.route("/panel/api/run", methods=["POST"])
@panel_required
def panel_run():
    data = request.get_json(force=True, silent=True) or {}
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify({"error": "命令为空"}), 400
    started = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=os.path.expanduser("~"),
                           capture_output=True, text=True, timeout=30,
                           executable="/bin/bash")
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes)
               else (e.stdout or "")) + "\n(命令超时 30 秒，已终止)"
        code = -1
    return jsonify({"exit_code": code, "output": out[-20000:],
                    "elapsed_sec": round(time.time() - started, 2)})



def _jsonable(obj):
    """递归转换 numpy 类型与非有限浮点数为 JSON 可序列化值。"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "item"):
        try:
            return _jsonable(obj.item())
        except Exception:
            return str(obj)
    return obj

DEFAULT_SYMBOLS = "600519,000001,300750,688981,00700,09988,NVDA,AAPL,TSLA"
JOBS = {}


class Args:
    def __init__(self, top=20, min_change=0.0, min_relvol=0.0):
        self.top = top
        self.min_change = min_change
        self.min_relvol = min_relvol


def run_scan_job(job_id, symbols, top, min_change, min_relvol, mode, market):
    JOBS[job_id]["status"] = "running"
    try:
        args = Args(top=top, min_change=min_change, min_relvol=min_relvol)
        scanner.stats = {"ok": 0, "fail": 0, "errors": [], "sources": set(),
                         "quote_times": set()}
        report = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
        if mode == "options":
            tk = (symbols.split(",")[0] if symbols else "").strip().upper() or "NVDA"
            report["options_unusual"] = {tk: scanner.options_unusual(tk, top)}
        elif mode == "limit_up":
            report["cn_limit_up_pool"] = scanner.scan_cn_full(args)
        else:
            syms = [s for s in symbols.split(",") if s.strip()]
            report["watchlist"] = scanner.scan_watchlist(syms, args)
        report["meta"] = {
            "sources": sorted(scanner.stats["sources"]),
            "quote_times": sorted(scanner.stats["quote_times"]),
            "success": scanner.stats["ok"], "failed": scanner.stats["fail"],
            "errors": scanner.stats["errors"],
            "notes": ["行情时间为数据源自带时间戳，非接口响应时间",
                      "Vol/OI>1 仅为成交活跃信号，不认定为机构买入"],
        }
        JOBS[job_id].update({"status": "done", "report": report})
    except Exception as e:  # noqa: BLE001
        JOBS[job_id].update({"status": "error", "error": f"{type(e).__name__}: {e}"})


@app.route("/")
def index():
    return render_template("index.html", default_symbols=DEFAULT_SYMBOLS)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(force=True, silent=True) or {}
    symbols = (data.get("symbols") or DEFAULT_SYMBOLS).strip()
    mode = (data.get("mode") or "watchlist").strip()
    top = int(data.get("top") or 20)
    min_change = float(data.get("min_change") or 0)
    min_relvol = float(data.get("min_relvol") or 0)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "pending", "started_at": time.time()}
    t = threading.Thread(target=run_scan_job,
                         args=(job_id, symbols, top, min_change, min_relvol, mode, "CN"),
                         daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/result/<job_id>")
def api_result(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(_jsonable(job))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
