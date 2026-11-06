#!/usr/bin/env python3
"""统一股票异动扫描器（沪深A股 / 港股 / 美股）。

复用已安装的四个 Skill 的数据源与方法，不重新安装：
  行情批量: 新浪 hq.sinajs.cn（A股/港股/美股，单次请求多标的）
  A股K线: 腾讯 ifzq.gtimg.cn（tencent_kline 同源，前复权）
  港美股K线: Yahoo chart API（stock_kline_yahoo 同源）
  美股期权: CBOE delayed quotes（options_chain_cboe 同源，仅个人研究）
  A股涨停池: 同花顺 data.10jqka.com.cn（ths_limit_up_pool 同源）
指标: 本地 stockstats（MA/MACD/RSI/KDJ/BOLL）+ 自算相对成交量。

用法示例：
  scan.py --symbols 600519,000001,00700,NVDA
  scan.py --market CN --full --top 20
  scan.py --symbols NVDA --options NVDA --top 10
  scan.py --symbols 00700,09988 --min-change 2 --min-relvol 1.5

输出: 人类可读报告 + JSON（--json）。每次扫描末尾列出数据源、
行情时间、成功/失败数、缺失字段。
"""
import argparse, json, re, sys, time
from datetime import datetime, timezone

import requests
import pandas as pd
from stockstats import StockDataFrame

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SINA = "https://hq.sinajs.cn/list="
HDR = {"Referer": "https://finance.sina.com.cn/", "User-Agent": UA}

stats = {"ok": 0, "fail": 0, "errors": [], "sources": set(), "quote_times": set()}


def note(ok, err=""):
    if ok:
        stats["ok"] += 1
    else:
        stats["fail"] += 1
        stats["errors"].append(err)


def sina_key(sym):
    """统一代码 -> 新浪 key"""
    s = sym.strip().upper()
    if s.endswith(".HK"):
        return "hk" + s[:-3].zfill(5)
    if re.fullmatch(r"\d{4,5}", s):
        return "hk" + s.zfill(5)
    if re.fullmatch(r"\d{6}", s):
        return ("sh" if s[0] in "69" or s.startswith("688") else "sz") + s
    return "gb_" + s.lower()


def market_of(sym):
    s = sym.strip().upper()
    if s.endswith(".HK") or re.fullmatch(r"\d{4,5}", s):
        return "HK"
    if re.fullmatch(r"\d{6}", s):
        return "CN"
    return "US"


def fetch_quotes(symbols):
    """批量行情，一次 HTTP 请求"""
    keys = [sina_key(s) for s in symbols]
    try:
        r = requests.get(SINA + ",".join(keys), headers=HDR, timeout=15)
        r.encoding = "gbk"
        stats["sources"].add("新浪 hq.sinajs.cn（实时行情）")
    except Exception as e:
        for s in symbols:
            note(False, f"{s} 行情请求失败: {type(e).__name__}")
        return {}
    out = {}
    for sym, key in zip(symbols, keys):
        m = re.search(r'hq_str_' + re.escape(key) + r'="([^"]*)"', r.text)
        if not m or not m.group(1):
            note(False, f"{sym} 行情空响应")
            continue
        f = m.group(1).split(",")
        mk = market_of(sym)
        try:
            if mk == "CN" and len(f) >= 32 and f[3]:
                out[sym] = {"market": mk, "name": f[0], "price": float(f[3]),
                            "prev_close": float(f[2]), "open": float(f[1]),
                            "high": float(f[4]), "low": float(f[5]),
                            "volume": f[8], "amount": f[9],
                            "change_pct": round((float(f[3]) - float(f[2])) / float(f[2]) * 100, 2),
                            "ts": f"{f[30]} {f[31]}".strip()}
            elif mk == "HK" and len(f) >= 19 and f[6]:
                out[sym] = {"market": mk, "name": f[1], "price": float(f[6]),
                            "prev_close": float(f[3]), "open": float(f[2]),
                            "high": float(f[4]), "low": float(f[5]),
                            "change_pct": float(f[8]), "ts": f"{f[17]} {f[18]}".strip(),
                            "amount": f[12], "volume": f[13]}
            elif mk == "US" and len(f) >= 30 and f[1]:
                out[sym] = {"market": mk, "name": f[0], "price": float(f[1]),
                            "change_pct": float(f[2]), "ts": f[3].strip(),
                            "open": float(f[5]), "high": float(f[6]), "low": float(f[7]),
                            "volume": f[10], "prev_close": float(f[26])}
            else:
                note(False, f"{sym} 行情字段不足")
                continue
            stats["quote_times"].add(out[sym]["ts"])
            note(True)
        except (ValueError, IndexError) as e:
            note(False, f"{sym} 行情解析失败: {e}")
    return out


def kline_cn(sym, count=40):
    """A股K线：腾讯 ifzq（tencent_kline 同源）"""
    code = sym.strip()
    prefix = "sh" if (code[0] in "69" or code.startswith("688")) else "sz"
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{count},qfq"
    try:
        d = requests.get(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": UA},
                         timeout=15).json()
        stats["sources"].add("腾讯 ifzq.gtimg.cn（A股前复权日线）")
        items = (d.get("data", {}).get(prefix + code, {}).get("qfqday") or [])
        rows = [{"date": it[0], "open": float(it[1]), "close": float(it[2]),
                 "high": float(it[3]), "low": float(it[4]), "volume": float(it[5])}
                for it in items]
        if len(rows) < 20:
            note(False, f"{sym} K线不足20根")
            return None
        note(True)
        return rows
    except Exception as e:
        note(False, f"{sym} A股K线失败: {type(e).__name__}: {e}")
        return None


def kline_yahoo(sym, count=40):
    """港美股K线：Yahoo chart（stock_kline_yahoo 同源）"""
    s = sym.strip().upper()
    ysym = s if (s.endswith(".HK") or market_of(s) == "US") else s
    if market_of(sym) == "HK" and not ysym.endswith(".HK"):
        # Yahoo 港股用 4 位代码（0700.HK），不是 5 位（00700.HK 会 404）
        ysym = s.lstrip("0").zfill(4) + ".HK"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1d&range=3mo"
    try:
        d = requests.get(url, headers={"User-Agent": UA}, timeout=15).json()
        stats["sources"].add("Yahoo Finance chart API（港美股日线）")
        r = d["chart"]["result"][0]
        ts, q = r["timestamp"], r["indicators"]["quote"][0]
        rows = []
        for i in range(len(ts)):
            if q["close"][i] is None:
                continue
            rows.append({"date": datetime.fromtimestamp(ts[i], tz=timezone.utc).strftime("%Y-%m-%d"),
                         "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
                         "close": q["close"][i], "volume": q["volume"][i] or 0})
        rows = rows[-count:]
        if len(rows) < 20:
            note(False, f"{sym} K线不足20根")
            return None
        note(True)
        return rows
    except Exception as e:
        note(False, f"{sym} YahooK线失败: {type(e).__name__}: {e}")
        return None


def indicators(rows):
    """MA5/10/20, MACD, RSI14, BOLL(20,2), 相对成交量"""
    df = pd.DataFrame(rows)
    sdf = StockDataFrame.retype(df.copy())
    c = df["close"]
    out = {"MA5": round(c.rolling(5).mean().iloc[-1], 2),
           "MA10": round(c.rolling(10).mean().iloc[-1], 2),
           "MA20": round(c.rolling(20).mean().iloc[-1], 2),
           "MACD_dif": round(float(sdf["macd"].iloc[-1]), 4),
           "MACD_dea": round(float(sdf["macds"].iloc[-1]), 4),
           "MACD_hist": round(float(sdf["macdh"].iloc[-1]), 4),
           "RSI14": round(float(sdf["rsi_14"].iloc[-1]), 2),
           "BOLL_mid": round(float(sdf["boll"].iloc[-1]), 2),
           "BOLL_up": round(float(sdf["boll_ub"].iloc[-1]), 2),
           "BOLL_dn": round(float(sdf["boll_lb"].iloc[-1]), 2)}
    v = df["volume"]
    avg20 = v.rolling(20).mean().iloc[-1]
    out["rel_volume"] = round(v.iloc[-1] / avg20, 2) if avg20 else None
    out["vol20_avg"] = round(avg20, 0) if avg20 else None
    # 价格相对 BOLL 位置 0~1
    up, dn = out["BOLL_up"], out["BOLL_dn"]
    out["boll_pos"] = round((c.iloc[-1] - dn) / (up - dn), 2) if up != dn else 0.5
    return out


def limit_zone(sym):
    """A股涨停板幅度：主板10%，创业板/科创板20%，北交所30%"""
    s = sym.strip()
    if s.startswith(("300", "301", "688")):
        return 0.20
    if s.startswith("920") or (len(s) == 6 and s[0] in "48"):
        return 0.30
    return 0.10


def limit_analysis(sym, quote, rows):
    """涨停 / 炸板 / 连板（基于日线收盘价序列回溯）"""
    if market_of(sym) != "CN" or not quote or not rows:
        return {}
    z = limit_zone(sym)
    closes = [r["close"] for r in rows]
    pcts = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    # 连板：从最近一天往前数连续涨停
    streak = 0
    for p in reversed(pcts):
        if p >= z * 0.98:
            streak += 1
        else:
            break
    limit_price = quote["prev_close"] * (1 + z)
    is_limit = quote["change_pct"] >= z * 100 * 0.98
    # 炸板：最高触及涨停价但收盘未封住
    fried = (quote["high"] >= limit_price * 0.995) and not is_limit
    return {"limit_pct": round(z * 100, 1), "is_limit_up": is_limit,
            "is_fried": fried, "limit_streak": streak,
            "limit_price": round(limit_price, 2)}


def anomaly_score(quote, ind):
    """综合异动评分 0~100：涨跌幅40 + 相对成交量30 + RSI极端15 + BOLL位置15"""
    chg = abs(quote.get("change_pct") or 0)
    s_chg = min(chg / 10 * 40, 40)
    rv = ind.get("rel_volume") or 0
    s_vol = min(max(rv - 1, 0) / 4 * 30, 30)
    rsi = ind.get("RSI14") or 50
    s_rsi = min(abs(rsi - 50) / 50 * 15, 15)
    bp = ind.get("boll_pos")
    s_boll = 0 if bp is None else min(abs(bp - 0.5) * 2 * 15, 15)
    return round(s_chg + s_vol + s_rsi + s_boll, 1)


def breakout_hk_us(quote, ind, rows):
    """港美股：异常成交额/放量突破。板块相对强弱需指数对照，此处返回个股信号。"""
    closes = [r["close"] for r in rows]
    hi20 = max(closes[-20:])
    return {"vol_breakout": (ind.get("rel_volume") or 0) >= 2.0,
            "price_breakout_20d": quote["price"] >= hi20 * 0.995,
            "high_20d": round(hi20, 2)}


def options_unusual(ticker, top=10):
    """美股期权异动（CBOE delayed quotes，options_chain_cboe 同源）。
    Vol/OI>1 仅为成交活跃信号，不认定为机构买入；成交量为合约级聚合值，非逐笔大单。"""
    import re as _re
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker.upper()}.json"
    try:
        d = requests.get(url, headers={"User-Agent": UA}, timeout=20).json()
        stats["sources"].add("CBOE delayed quotes（美股期权链，仅个人研究）")
    except Exception as e:
        note(False, f"{ticker} 期权链失败: {type(e).__name__}: {e}")
        return None
    osi = _re.compile(r"^(?P<root>[A-Z][A-Z0-9]*)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")
    hits = []
    for o in (d.get("data") or {}).get("options") or []:
        m = osi.match(o.get("option", ""))
        if not m:
            continue
        g = m.groupdict()
        v, oi = o.get("volume") or 0, o.get("open_interest") or 0
        if oi > 0 and v > 0 and v / oi > 1:
            hits.append({"expiry": f"20{g['y']}-{g['m']}-{g['d']}",
                         "type": "call" if g["cp"] == "C" else "put",
                         "strike": int(g["strike"]) / 1000,
                         "volume": v, "oi": oi, "vol_oi": round(v / oi, 2),
                         "iv": o.get("iv"), "delta": o.get("delta"),
                         "gamma": o.get("gamma")})
    hits.sort(key=lambda x: x["volume"], reverse=True)
    ts = d.get("timestamp")
    if ts:
        stats["quote_times"].add(str(ts))
    note(True)
    return {"timestamp": ts, "count": len(hits), "top": hits[:top]}


def scan_watchlist(symbols, args):
    quotes = fetch_quotes(symbols)
    results = []
    for sym in symbols:
        q = quotes.get(sym)
        if not q:
            continue
        mk = q["market"]
        rows = kline_cn(sym) if mk == "CN" else kline_yahoo(sym)
        if not rows:
            continue
        ind = indicators(rows)
        item = {"symbol": sym, "market": mk, "quote": q, "indicators": ind,
                "score": anomaly_score(q, ind)}
        if mk == "CN":
            item["limit"] = limit_analysis(sym, q, rows)
        else:
            item["breakout"] = breakout_hk_us(q, ind, rows)
        results.append(item)
    # 筛选
    if args.min_change:
        results = [r for r in results if abs(r["quote"].get("change_pct") or 0) >= args.min_change]
    if args.min_relvol:
        results = [r for r in results if (r["indicators"].get("rel_volume") or 0) >= args.min_relvol]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:args.top]


def scan_cn_full(args):
    """A股全市场：同花顺涨停池（ths_limit_up_pool 同源），按异动信号排序"""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    params = {"page": 1, "limit": 200,
              "field": "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004",
              "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0", "date": today}
    try:
        info = (requests.get(url, params=params, headers={"User-Agent": UA}, timeout=12)
                .json().get("data") or {}).get("info", [])
        stats["sources"].add("同花顺 limit_up_pool（A股涨停池）")
        stats["quote_times"].add(today)
        note(True)
    except Exception as e:
        note(False, f"A股涨停池失败: {type(e).__name__}: {e}")
        return []
    out = []
    for it in info:
        # 同花顺返回命名字段（非数字key）
        out.append({"symbol": str(it.get("code", "")),
                    "name": it.get("name"), "price": it.get("latest"),
                    "change_pct": it.get("change_rate"),
                    "reason": it.get("reason_type"),
                    "board_type": it.get("limit_up_type"),
                    "seal_rate": it.get("limit_up_suc_rate"),
                    "high_days": it.get("high_days"),
                    "is_again": it.get("is_again_limit"),
                    "first_time": it.get("first_limit_up_time"),
                    "seal_amount": it.get("order_amount"),
                    "turnover_rate": it.get("turnover_rate")})
    return out[:args.top]


def main():
    ap = argparse.ArgumentParser(description="统一股票异动扫描器")
    ap.add_argument("--symbols", default="", help="自选股，逗号分隔，如 600519,00700,NVDA")
    ap.add_argument("--market", default="", help="CN/HK/US，配合 --full 使用")
    ap.add_argument("--full", action="store_true", help="全市场扫描（当前仅支持 CN 涨停池）")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-change", type=float, default=0, help="最小涨跌幅绝对值过滤")
    ap.add_argument("--min-relvol", type=float, default=0, help="最小相对成交量过滤")
    ap.add_argument("--options", default="", help="美股期权异动扫描 ticker，如 NVDA")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
    if args.options:
        report["options_unusual"] = {args.options: options_unusual(args.options, args.top)}
    if args.full and args.market.upper() == "CN":
        report["cn_limit_up_pool"] = scan_cn_full(args)
    if args.symbols:
        syms = [s for s in args.symbols.split(",") if s.strip()]
        report["watchlist"] = scan_watchlist(syms, args)
    if not args.options and not args.symbols and not args.full:
        ap.error("请指定 --symbols、--options 或 --market CN --full")

    # 缺失字段统计
    missing = []
    for sec in ("watchlist",):
        for r in report.get(sec) or []:
            q = r["quote"]
            for k in ("volume", "amount"):
                if k not in q:
                    missing.append(f"{r['symbol']}.{k}")
    report["meta"] = {
        "sources": sorted(stats["sources"]),
        "quote_times": sorted(stats["quote_times"]),
        "success": stats["ok"], "failed": stats["fail"],
        "errors": stats["errors"],
        "missing_fields": sorted(set(missing)) or ["无（本次扫描字段齐全）"],
        "notes": ["行情时间为数据源自带时间戳，非接口响应时间",
                  "Vol/OI>1 仅为成交活跃信号，不认定为机构买入",
                  "期权成交量为合约级聚合值，非逐笔大单",
                  "CBOE/Yahoo 数据仅供个人研究"],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return

    # 人类可读输出
    print(f"=== 异动扫描报告 {report['generated_at']} ===")
    for r in report.get("watchlist") or []:
        q, ind = r["quote"], r["indicators"]
        print(f"\n[{r['score']:>5}] {r['symbol']} {q['name']} {q['price']} "
              f"{q.get('change_pct', '?')}%  RSI{ind['RSI14']} 量比{ind['rel_volume']}")
        print(f"      MA5={ind['MA5']} MA10={ind['MA10']} MA20={ind['MA20']} "
              f"MACD={ind['MACD_hist']} BOLL[{ind['BOLL_dn']},{ind['BOLL_up']}]位置{ind['boll_pos']}")
        if "limit" in r and r["limit"]:
            L = r["limit"]
            tags = []
            if L["is_limit_up"]:
                tags.append(f"涨停({L['limit_streak']}连板)")
            if L["is_fried"]:
                tags.append("炸板")
            print(f"      A股: 涨停线{L['limit_pct']}% " + " ".join(tags) if tags else f"      A股: 涨停线{L['limit_pct']}%")
        if "breakout" in r:
            b = r["breakout"]
            tags = [t for t, v in [("放量", b["vol_breakout"]), ("突破20日高", b["price_breakout_20d"])] if v]
            if tags:
                print(f"      突破信号: {' '.join(tags)} (20日高{b['high_20d']})")
    for it in report.get("cn_limit_up_pool") or []:
        print(f"\n  {it['symbol']} {it['name']} {it['price']} {it['change_pct']}% "
              f"[{it['board_type']}] {it['high_days']} 封板率{it['seal_rate']} "
              f"换手{it['turnover_rate']}% 原因:{it['reason']}")
    for tk, o in (report.get("options_unusual") or {}).items():
        if not o:
            continue
        print(f"\n--- {tk} 期权异动 (Vol/OI>1, {o['count']}个, 快照{ o['timestamp']}) ---")
        for c in o["top"]:
            print(f"  {c['expiry']} {c['type']:4s} K={c['strike']:<9} vol={c['volume']:<7} "
                  f"OI={c['oi']:<7} Vol/OI={c['vol_oi']:<6} IV={c['iv']} delta={c['delta']}")
    m = report["meta"]
    print("\n--- 扫描元信息 ---")
    print(f"数据源: {'; '.join(m['sources'])}")
    print(f"行情时间: {'; '.join(m['quote_times'])}")
    print(f"成功 {m['success']} / 失败 {m['failed']}")
    if m["errors"]:
        print("错误: " + "; ".join(m["errors"][:5]))
    print("缺失字段: " + "; ".join(m["missing_fields"]))
    for n in m["notes"]:
        print(f"注: {n}")


if __name__ == "__main__":
    main()
