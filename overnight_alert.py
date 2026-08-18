"""Overnight SPX gap alert. Runs on GitHub Actions cron, pushes to ntfy.

Independent of the Mac. Reference and live price both come off the same
TradingView SPX500 feed, never mixed vendors: a cross-vendor basis offset on a
CFD would fire false gaps. State is committed back to the repo because Actions
runs are stateless and an in-memory set would re-alert every tier every run.
"""
import datetime as dt
import json
import os
import random
import re
import string
import threading
import urllib.request
from zoneinfo import ZoneInfo

import websocket as _ws

TIERS = (1.00, 1.50, 2.00)
STATE = "state.json"
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
ET = ZoneInfo("America/New_York")


def tv_history(tf="5", bars=300, symbol="FOREXCOM:SPX500", timeout=25):
    out, err = [], []
    def _r(p): return p + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    cs = _r("cs_")
    def on_open(ws):
        def send(m, p):
            msg = json.dumps({"m": m, "p": p}); ws.send(f"~m~{len(msg)}~m~{msg}")
        send("set_auth_token", ["unauthorized_user_token"])
        send("chart_create_session", [cs, ""])
        send("resolve_symbol", [cs, "sds_sym_1",
                                '={"symbol":"%s","adjustment":"splits"}' % symbol])
        send("create_series", [cs, "sds_1", "s1", "sds_sym_1", tf, bars, ""])
    def on_message(ws, message):
        for pkt in re.findall(r"~m~\d+~m~(.+?)(?=~m~\d+~m~|$)", message, re.DOTALL):
            if pkt.startswith("~h~"):
                ws.send(f"~m~{len(pkt)}~m~{pkt}")
                continue
            try:
                d = json.loads(pkt)
            except Exception:
                continue
            m = d.get("m")
            if m in ("critical_error", "protocol_error", "series_error", "symbol_error"):
                err.append(d); ws.close()
            elif m in ("timescale_update", "du"):
                for v in d["p"][1:]:
                    if not isinstance(v, dict):
                        continue
                    for k in v:
                        if isinstance(v[k], dict) and "s" in v[k]:
                            for b in v[k]["s"]:
                                a = b["v"]
                                out.append({"t": int(a[0]), "o": a[1], "h": a[2],
                                            "l": a[3], "c": a[4]})
            elif m == "series_completed":
                ws.close()
    def on_error(ws, e): err.append(str(e)); ws.close()
    try:
        ws = _ws.WebSocketApp(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F&type=chart",
            header={"Origin": "https://www.tradingview.com"},
            on_open=on_open, on_message=on_message, on_error=on_error)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start(); t.join(timeout)
        try: ws.close()
        except Exception: pass
    except Exception as e:
        err.append(str(e))
    seen = {b["t"]: b for b in out}
    return [seen[k] for k in sorted(seen)], err


def push(title, body, priority="high", tags="chart_with_downwards_trend"):
    req = urllib.request.Request(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Priority": priority, "Tags": tags},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def market_open(et):
    """Cash session, when this monitor stands down."""
    return et.weekday() < 5 and dt.time(9, 30) <= et.time() < dt.time(16, 0)


def cme_closed(et):
    """CME weekend: Fri 17:00 ET to Sun 18:00 ET."""
    if et.weekday() == 4 and et.time() >= dt.time(17, 0):
        return True
    if et.weekday() == 5:
        return True
    if et.weekday() == 6 and et.time() < dt.time(18, 0):
        return True
    return False


def session_key(et):
    """Named for the cash close it follows."""
    if et.time() >= dt.time(16, 0) and et.weekday() < 5:
        return et.date().isoformat()
    d = et.date() - dt.timedelta(days=1)
    while d.weekday() > 4:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def cash_close(bars, sess):
    """The 16:00 ET print. 5m bars are stamped at their OPEN, so the bar stamped
    15:55 ET closes at the 16:00 print. Daily bars are unusable here: TradingView
    keeps the daily bar open until 17:00 ET so its close tracks the live price."""
    tgt = dt.datetime.combine(dt.date.fromisoformat(sess), dt.time(15, 55),
                              tzinfo=ET).timestamp()
    exact = [b for b in bars if b["t"] == tgt]
    if exact:
        return exact[0]["c"]
    near = [b for b in bars if b["t"] <= tgt]
    if not near or tgt - near[-1]["t"] > 1800:
        return None
    return near[-1]["c"]


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def main():
    et = dt.datetime.now(ET)
    print(f"ET now {et:%Y-%m-%d %H:%M} ({et:%a})")
    if market_open(et):
        print("cash session open, standing down")
        return
    if cme_closed(et):
        print("CME weekend, standing down")
        return

    sess = session_key(et)
    st = load_state()
    if st.get("session") != sess:
        st = {"session": sess, "ref": None, "fired": [], "blind_notified": False}

    bars, err = tv_history()
    if not bars:
        print(f"tradingview returned nothing: {err[:1]}")
        if not st.get("blind_notified"):
            push("Overnight monitor blind",
                 f"No TradingView data for session {sess}. Gap alerts are NOT running.",
                 priority="default", tags="warning")
            st["blind_notified"] = True
            json.dump(st, open(STATE, "w"), indent=1)
        return

    if st.get("ref") is None:
        ref = cash_close(bars, sess)
        if ref is None:
            print(f"no 5m bar within 30min of the {sess} cash close, cannot set ref")
            return
        st["ref"] = ref
        print(f"ref set: {sess} cash close SPX {ref:.2f}")
    ref = st["ref"]

    px = bars[-1]["c"]
    last_bar = dt.datetime.fromtimestamp(bars[-1]["t"], ET)
    pct = 100.0 * (px / ref - 1.0)
    print(f"live SPX {px:.2f} (bar {last_bar:%H:%M} ET), ref {ref:.2f}, {pct:+.2f}%")

    hit = [t for t in TIERS if abs(pct) >= t and t not in st["fired"]]
    if hit:
        tier = max(hit)
        st["fired"] = sorted(set(st["fired"]) | {t for t in TIERS if abs(pct) >= t})
        direction = "UP" if pct > 0 else "DOWN"
        fv = float(os.environ.get("ES_FV", "0") or 0)
        body = (f"SPX {px:.0f} (ES {px + fv:.0f}) is {direction} {abs(pct):.2f}% "
                f"({abs(px - ref):.0f} pts) from the {sess} cash close "
                f"SPX {ref:.0f} (ES {ref + fv:.0f}).")
        code = push(f"Overnight {direction} {abs(pct):.2f}%", body,
                    priority="urgent" if tier >= 1.0 else "high",
                    tags="rotating_light" if tier >= 1.0 else "chart_with_downwards_trend")
        print(f"pushed tier {tier:.2f}% (ntfy {code}): {body}")

    json.dump(st, open(STATE, "w"), indent=1)


if __name__ == "__main__":
    main()
