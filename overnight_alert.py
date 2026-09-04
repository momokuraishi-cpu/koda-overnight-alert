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


# ── fair value premium ────────────────────────────────────────────────────────
# Ported from daily_levels.py measured_fv(). A hardcoded ES_FV was 22.89 against
# a real basis of 7.32 on 2026-09-04: the premium decays into expiry and jumps
# at every quarterly roll, so any constant is wrong within weeks.
NET_CARRY = 0.035   # risk-free (~4.5%) minus dividend yield (~1.0%), fallback only


def es_front_expiry(today=None):
    """3rd Friday of the front Mar/Jun/Sep/Dec quarter. Survives contract rolls."""
    today = today or dt.date.today()
    for yr_offset in range(3):
        yr = today.year + yr_offset
        for m in (3, 6, 9, 12):
            if yr == today.year and m < today.month:
                continue
            first = dt.date(yr, m, 1)
            first_fri = first + dt.timedelta(days=(4 - first.weekday()) % 7)
            third_fri = first_fri + dt.timedelta(weeks=2)
            if third_fri >= today:
                return third_fri
    return dt.date(today.year + 1, 3, 21)


def es_tv_symbol(expiry):
    return "CME_MINI:ES%s%d" % ({3: "H", 6: "M", 9: "U", 12: "Z"}[expiry.month],
                                expiry.year)


def _med(xs):
    s = sorted(xs)
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def measured_fv(cash_bars, slots=24):
    """Basis measured as ES minus the SPX500 feed, off matched 5m bars.

    Measured against SPX500 and NOT SP:SPX on purpose. The alert reports
    px + fv where px is the SPX500 close, so the fv that makes that arithmetic
    correct is ES minus SPX500. The CFD sits ~1.8pts above cash SPX, so the
    9.10 that daily_levels measures against SP:SPX would overstate ES here.
    SP:SPX is also unusable overnight: it prints flat repeated bars, which is
    the unmatched-timestamp mistake wearing a timestamp.

    Returns (fv, note) or (None, reason).
    """
    expiry = es_front_expiry()
    es, err = tv_history(tf="5", bars=400, symbol=es_tv_symbol(expiry), timeout=30)
    if not es:
        return None, f"no ES bars for {es_tv_symbol(expiry)}: {err[:1]}"

    cash_by_t = {b["t"]: b for b in cash_bars if b["h"] != b["l"]}
    basis = [b["c"] - cash_by_t[b["t"]]["c"]
             for b in sorted(es, key=lambda x: x["t"]) if b["t"] in cash_by_t]
    if len(basis) < slots:
        return None, f"only {len(basis)} matched live slots"

    # The last bar of a cash session reads 3-5pts wide because ES keeps trading.
    # Median first, then drop anything more than 1.5pts off it.
    tail = basis[-slots:]
    m0 = _med(tail)
    keep = [b for b in tail if abs(b - m0) <= 1.5]
    if len(keep) < slots // 2:
        return None, f"basis unstable, only {len(keep)}/{len(tail)} slots agree"
    fv = _med(keep)
    if not (0 < fv < 120):
        return None, f"implausible basis {fv:.2f}"
    return fv, f"measured, median of {len(keep)}/{len(tail)} matched 5m slots"


def fair_value_premium(cash_bars, spx_price):
    """measured -> modelled -> ES_FV env var. Never raises: a wrong ES number in
    the body must not swallow the gap alert itself."""
    try:
        fv, note = measured_fv(cash_bars)
    except Exception as e:
        fv, note = None, f"measure raised: {e}"
    if fv is not None:
        return fv, note
    days = (es_front_expiry() - dt.date.today()).days
    modelled = spx_price * NET_CARRY * days / 365
    if 0 < modelled < 120:
        return modelled, f"modelled from NET_CARRY over {days}d ({note})"
    env = float(os.environ.get("ES_FV", "0") or 0)
    return env, f"fell back to ES_FV env ({note})"


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
        fv, fv_note = fair_value_premium(bars, px)
        print(f"fv {fv:.2f}pts ({fv_note})")
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
