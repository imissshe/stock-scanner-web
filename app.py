#!/usr/bin/env python3
"""股票异动扫描器网页版：Flask 包装 scan.py，对外提供扫描页面与 API。"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import scan as scanner

app = Flask(__name__)

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
        if mode == "limit_up":
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
    return jsonify(job)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
