"""
Shared helpers for the Gold & Macro Monitor scripts.
Mirrors the logic from the Google Apps Script version, ported to Python.
"""
import datetime
import html
import os
import re
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

CARRY_TRADE_FRAMEWORK = (
    "BACKGROUND FRAMEWORK - apply this explicitly whenever relevant, do not just gesture at "
    '"rates matter":\n'
    "- The Fed-BOJ policy rate differential drives the USD/JPY carry trade (investors borrow "
    "cheap yen to fund higher-yielding dollar assets). A WIDENING differential (Fed hawkish/"
    "holding, BOJ dovish/slow) fuels more carry trade activity and typically supports the dollar, "
    "which is a headwind for gold. A NARROWING differential (BOJ hiking, or Fed turning dovish) "
    "reduces the trade's attractiveness and raises the risk of a carry-trade unwind.\n"
    "- A carry-trade unwind is a distinct, sometimes opposite-signed risk from the \"higher real "
    "yields hurt gold\" channel: an unwind is typically a risk-off, volatile event that can drive "
    "safe-haven flows INTO gold even while the same underlying hawkishness pushes real yields up. "
    "When headlines mention BOJ policy moves, yen intervention, or a sharp USD/JPY move, explicitly "
    "weigh which of these two channels (higher-real-yields-hurts-gold vs. unwind-risk-off-helps-gold) "
    "is more likely dominant right now, rather than only applying one.\n"
    "- FX intervention itself (BOJ/MOF buying yen) does not directly move US Treasury yields - its "
    "main effect is signaling the central banks' shared discomfort with the rate divergence, which "
    "shapes market expectations about future policy paths. Do not treat intervention headlines as if "
    "they mechanically move US yields on their own."
)

ANALYSIS_STYLE_GUIDE = (
    "Write like an experienced macro/futures analyst briefing a trader who already "
    "understands the market - not like a news summary. Do not just restate the inputs "
    "back as a list. Structure your response in five short sections with these exact "
    "headers:\n"
    "1. WHAT CHANGED - the one or two things that actually matter from the input, and why.\n"
    "2. REAL YIELD / RATE LINKAGE - reason through how this connects to US real yields "
    "(nominal rates minus inflation expectations), since that is the dominant driver of gold.\n"
    "3. DIRECTIONAL VIEW - give a clear lean (bullish / bearish / neutral-range) for gold "
    "over the next 1-2 weeks, with a rough confidence level (low/medium/high) and the single "
    "biggest reason for that lean. You are also given a PIN-BAR LEVEL SETUP CHECK below, which "
    "is a specific, already-decided trading rule (not something for you to re-derive) - if it "
    "says price is at a live decision point, you MUST explicitly state its confirmation/rejection "
    "verdict and weave it into your near-term view; if it says no level is active, say so "
    "explicitly rather than inventing one.\n"
    "4. WHY NOT THE OPPOSITE CASE - state the strongest argument for the opposite direction "
    "(e.g. if you lean bearish, give the honest bull case) and then explain specifically why "
    "the current data does not make that the higher-probability outcome right now. If the data "
    "shows a retracement or pullback, explicitly address whether that looks like a genuine trend "
    "reversal or a normal corrective move within a larger trend, and justify which one using the "
    "specific numbers given - do not just assert \"it is just a pullback\" without reasoning.\n"
    "5. WHAT WOULD CHANGE MY MIND - the specific data point or event that would actually flip the view.\n"
    "Keep the whole thing under 450 words. Be decisive but honest about uncertainty - do not "
    "hedge every sentence, but do not overstate confidence either. This is analysis to inform "
    "a decision, not investment advice, and you can note that briefly at the end.\n\n"
    "CRITICAL PRICE RULE: only reference the exact current price and Fibonacci levels given to "
    "you explicitly in the CURRENT PRICE DATA section below - never invent, round differently, "
    "or state any other specific price level. If that section says price data is unavailable, "
    "do not state any specific price or price range at all - describe direction only in "
    "relative terms (e.g. 'further downside pressure from current levels')."
)

SECTION_HEADERS = [
    ("changed", r"\**\s*1\.\s*WHAT CHANGED\s*\**:?"),
    ("yield", r"\**\s*2\.\s*REAL YIELD.*?LINKAGE\s*\**:?"),
    ("direction", r"\**\s*3\.\s*DIRECTIONAL VIEW\s*\**:?"),
    ("opposite", r"\**\s*4\.\s*WHY NOT THE OPPOSITE CASE\s*\**:?"),
    ("mind", r"\**\s*5\.\s*WHAT WOULD CHANGE MY MIND\s*\**:?"),
]

SECTION_META = {
    "changed": {"label": "What Changed", "icon": "\U0001F4CC", "color": "#34495e"},
    "yield": {"label": "Real Yield / Rate Linkage", "icon": "\U0001F4B5", "color": "#2980b9"},
    "opposite": {"label": "Why Not The Opposite Case", "icon": "\U0001F50E", "color": "#8e44ad"},
    "mind": {"label": "What Would Change My Mind", "icon": "\u26A0\uFE0F", "color": "#b7791f"},
}


def escape_html(s):
    return html.escape(str(s), quote=False)


def ask_gemini(prompt, max_retries=3):
    """Calls the Gemini free-tier API. Model name is read from GEMINI_MODEL env var (falls
    back to the default whether the var is unset OR set-but-empty) so it can be overridden
    via a GitHub secret without touching code. Retries automatically on transient
    "high demand" / overload errors, since those usually clear up within seconds."""
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    last_error = None
    for attempt in range(1, max_retries + 1):
        data = None
        try:
            resp = requests.post(url, params={"key": api_key}, json=payload, timeout=60)
            data = resp.json()
        except requests.RequestException as e:
            last_error = f"[Gemini request failed: {e}]"

        if data is not None:
            if "error" not in data:
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError):
                    last_error = f"[Could not parse Gemini response: {str(data)[:500]}]"
            else:
                msg = data["error"].get("message", "")
                last_error = f"[Gemini error: {msg}]"
                if "high demand" not in msg.lower() and "overloaded" not in msg.lower():
                    break  # a real error, not just overload - no point retrying

        if attempt < max_retries:
            wait = 20 * attempt
            print(f"Attempt {attempt} failed ({last_error}). Retrying in {wait}s...")
            time.sleep(wait)
    return last_error


def parse_sections(text):
    matches = []
    for key, pattern in SECTION_HEADERS:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            matches.append((m.start(), m.end(), key))
    matches.sort(key=lambda x: x[0])
    sections = {}
    for i, (start, end, key) in enumerate(matches):
        content_end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        sections[key] = text[end:content_end].strip()
    return sections


def direction_color(text):
    if re.search(r"bearish", text, re.IGNORECASE):
        return {"color": "#c0392b", "bg": "#fdecea", "label": "BEARISH"}
    if re.search(r"bullish", text, re.IGNORECASE):
        return {"color": "#1e8449", "bg": "#eafaf1", "label": "BULLISH"}
    return {"color": "#b7791f", "bg": "#fef9e7", "label": "NEUTRAL / MIXED"}


def text_to_html(text):
    """Turns plain text into real paragraphs + bullet lists instead of one blob."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    bullets = []

    def flush():
        nonlocal bullets
        if bullets:
            items = "".join(f'<li style="margin-bottom:5px;color:#333;">{b}</li>' for b in bullets)
            out.append(f'<ul style="margin:4px 0 10px 0;padding-left:20px;">{items}</ul>')
            bullets = []

    for line in lines:
        m = re.match(r"^[-*\u2022]\s+(.*)", line)
        if m:
            bullets.append(escape_html(m.group(1)))
        else:
            flush()
            out.append(f'<p style="margin:0 0 10px 0;color:#333;">{escape_html(line)}</p>')
    flush()
    return "".join(out)


def build_newsletter_html(title, subtitle, sections, raw_fallback, extra_html_before="", nav_links=None):
    order = ["changed", "yield", "direction", "opposite", "mind"]
    body = extra_html_before
    quick_take = ""

    if not sections:
        body += f'<div style="font-family:{FONT_STACK};font-size:14px;line-height:1.6;">{text_to_html(raw_fallback)}</div>'
    else:
        if "direction" in sections:
            c = direction_color(sections["direction"])
            first_sentence = re.split(r"[.!?]", sections["direction"])[0].strip()
            quick_take = (
                '<div style="text-align:center;margin:0 0 20px 0;">'
                f'<span style="display:inline-block;padding:7px 18px;border-radius:20px;background:{c["color"]};'
                f'color:#fff;font-size:12px;font-weight:bold;letter-spacing:0.8px;">\U0001F3AF {c["label"]}</span>'
                f'<div style="font-size:14px;color:#555;margin-top:10px;font-style:italic;max-width:480px;'
                f'margin-left:auto;margin-right:auto;">{escape_html(first_sentence)}.</div></div>'
                '<hr style="border:none;border-top:1px solid #eee;margin:0 0 18px 0;">'
            )
        for key in order:
            if key not in sections:
                continue
            content_html = text_to_html(sections[key])
            if key == "direction":
                c = direction_color(sections[key])
                body += (
                    f'<div style="margin:0 0 18px 0;padding:16px 18px;border-radius:10px;background:{c["bg"]};'
                    f'border-left:5px solid {c["color"]};">'
                    f'<div style="font-size:12px;font-weight:bold;letter-spacing:0.6px;color:{c["color"]};'
                    f'margin-bottom:8px;text-transform:uppercase;">\U0001F3AF Directional View</div>'
                    f'<div style="font-size:14px;line-height:1.6;">{content_html}</div></div>'
                )
            else:
                meta = SECTION_META[key]
                body += (
                    f'<div style="margin:0 0 18px 0;padding:14px 18px;border-left:4px solid {meta["color"]};'
                    f'background:#fafafa;border-radius:6px;">'
                    f'<div style="font-size:12px;font-weight:bold;letter-spacing:0.6px;color:{meta["color"]};'
                    f'margin-bottom:8px;text-transform:uppercase;">{meta["icon"]} {meta["label"]}</div>'
                    f'<div style="font-size:14px;line-height:1.6;">{content_html}</div></div>'
                )

    nav_html = ""
    if nav_links:
        pills = "".join(
            f'<a href="{escape_html(url)}" style="display:inline-block;padding:6px 14px;margin:0 6px;'
            f'border-radius:16px;background:#f0f0f5;color:#1a1a2e;font-size:12px;font-weight:600;'
            f'text-decoration:none;">{escape_html(label)}</a>'
            for label, url in nav_links
        )
        nav_html = f'<div style="text-align:center;padding:14px 0 0;">{pills}</div>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape_html(title)}</title></head>
<body style="margin:0;padding:24px;background:#f2f2f2;">
<div style="font-family:{FONT_STACK};max-width:600px;margin:0 auto;background:#ffffff;">
  <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:24px 24px 20px;border-radius:12px 12px 0 0;">
    <div style="color:#8ab4f8;font-size:11px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:6px;">Gold &amp; Macro Intelligence</div>
    <h1 style="color:#ffffff;margin:0;font-size:21px;font-weight:700;">{escape_html(title)}</h1>
    <div style="color:#a9b4c4;font-size:12px;margin-top:6px;">{escape_html(subtitle)}</div>
  </div>
  {nav_html}
  <div style="border:1px solid #eee;border-top:none;padding:22px 24px 20px;border-radius:0 0 12px 12px;">
    {quick_take}{body}
  </div>
  <div style="color:#999;font-size:11px;margin-top:14px;text-align:center;line-height:1.5;">
    Automatically generated &middot; Not investment advice
  </div>
</div>
</body></html>"""


def _twelvedata_request(endpoint, params):
    """
    Calls the Twelve Data API - a proper, documented, key-based market data
    provider (not a scraped or unofficial endpoint). Free tier: 800 calls/day,
    8/min, no credit card required. Get a key at https://twelvedata.com/register
    and set it as the TWELVEDATA_API_KEY secret.

    This replaced Yahoo's GC=F futures symbol, which turned out to be
    unreliable for this purpose: GC=F tracks whichever COMEX contract month
    happens to be "front month" at query time, and different contract months
    can trade hundreds of dollars apart due to the futures curve (contango) -
    causing exactly the "$4,462 instead of $4,417" kind of error this switch
    fixes. XAU/USD here is a proper spot-style forex-convention symbol.
    """
    api_key = os.environ.get("TWELVEDATA_API_KEY")
    if not api_key:
        print("TWELVEDATA_API_KEY is not set - price data will be unavailable.")
        return None
    url = f"https://api.twelvedata.com/{endpoint}"
    params = dict(params)
    params["apikey"] = api_key
    try:
        resp = requests.get(url, params=params, timeout=20)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"Twelve Data request to {endpoint} failed: {e}")
        return None
    if isinstance(data, dict) and data.get("status") == "error":
        print(f"Twelve Data API error on {endpoint}: {data.get('message')}")
        return None
    return data


def fetch_gold_price_data():
    """
    Fetches current gold spot price and yesterday's completed daily OHLC from
    Twelve Data's XAU/USD symbol, then computes Fibonacci levels the way the
    MT4 EA's Pin Zone logic does (FIB_PIN_BOTH): two separate sets across each
    wick - UPPER (candle body top -> day's high) and LOWER (candle body
    bottom -> day's low). This mirrors Fib_DrawPinZones() in the EA, applied
    to the most recent completed daily candle ("yesterday's pin bar").

    Returns a dict of structured values, or None if the fetch/parse fails or
    TWELVEDATA_API_KEY isn't set.
    """
    price_resp = _twelvedata_request("price", {"symbol": "XAU/USD"})
    if not price_resp or "price" not in price_resp:
        return None
    try:
        current_price = float(price_resp["price"])
    except (TypeError, ValueError):
        return None

    series = _twelvedata_request("time_series", {
        "symbol": "XAU/USD", "interval": "1day", "outputsize": 10, "order": "ASC",
    })
    if not series or "values" not in series:
        return None

    candles = []
    for v in series["values"]:
        try:
            candles.append({
                "date": v["datetime"],
                "open": float(v["open"]), "high": float(v["high"]),
                "low": float(v["low"]), "close": float(v["close"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    if not candles:
        return None

    # Skip today's bar if the API included a still-forming partial one, then
    # walk backward past any degenerate/flat bars (high == low) to find the
    # last genuinely complete daily candle - this is the same defensive check
    # that caught the earlier flat-candle bug, kept here in case any data
    # provider ever serves one.
    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    usable = [c for c in candles if c["date"] != today_str] or candles

    y = None
    for candidate in reversed(usable):
        if candidate["high"] > candidate["low"]:
            y = candidate
            break
    if y is None:
        return None

    body_top = max(y["open"], y["close"])
    body_bottom = min(y["open"], y["close"])

    def fib_set(zero_price, hundred_price):
        rng = hundred_price - zero_price
        return [
            ("0%", zero_price),
            ("23.6%", zero_price + rng * 0.236),
            ("38.2%", zero_price + rng * 0.382),
            ("50%", zero_price + rng * 0.5),
            ("61.8%", zero_price + rng * 0.618),
            ("78.6%", zero_price + rng * 0.786),
            ("100%", hundred_price),
        ]

    return {
        "current_price": current_price,
        "yesterday_date": y["date"],
        "open": y["open"], "high": y["high"], "low": y["low"], "close": y["close"],
        "body_top": body_top, "body_bottom": body_bottom,
        "upper_pin_fib": fib_set(body_top, y["high"]),      # upper wick
        "lower_pin_fib": fib_set(body_bottom, y["low"]),    # lower wick
    }


DISPLAY_FIB_LEVELS = ("50%", "61.8%", "78.6%")


def format_price_context(pd_):
    """Turns the structured dict from fetch_gold_price_data() into the human/prompt-readable
    text block. Kept separate from fetching so the same structured data can also feed the
    setup-check math without re-parsing text. Only shows the 50/61.8/78.6% levels to keep
    the report focused - the 0/23.6/38.2/100% anchors are still used internally (in
    fetch_gold_price_data and build_pin_bar_setup_note) but not surfaced here. The candle
    body (open/close) is used internally to anchor the wicks but isn't shown either - only
    the wick range itself (high/low) matters to the reader."""
    lines = [
        f"Current gold price (XAU/USD spot): ${pd_['current_price']:,.2f}",
        f"Yesterday's ({pd_['yesterday_date']}) range: High ${pd_['high']:,.2f} / Low ${pd_['low']:,.2f}",
        "",
        "UPPER PIN ZONE Fibonacci (upper wick, toward the day's high):",
    ]
    for label, level in pd_["upper_pin_fib"]:
        if label in DISPLAY_FIB_LEVELS:
            lines.append(f"  {label}: ${level:,.2f}")
    lines.append("")
    lines.append("LOWER PIN ZONE Fibonacci (lower wick, toward the day's low):")
    for label, level in pd_["lower_pin_fib"]:
        if label in DISPLAY_FIB_LEVELS:
            lines.append(f"  {label}: ${level:,.2f}")
    return "\n".join(lines)


def fetch_recent_30m_close():
    """
    Fetches the most recently CLOSED 30-minute candle's close price for
    XAU/USD via Twelve Data (index 1 of a descending-order series, since
    index 0 may still be forming). Used to confirm or reject the pin-bar
    setup below. Returns {"close": float, "time_label": str} or None.
    """
    series = _twelvedata_request("time_series", {
        "symbol": "XAU/USD", "interval": "30min", "outputsize": 5, "order": "DESC",
    })
    if not series or "values" not in series or len(series["values"]) < 2:
        return None
    try:
        v = series["values"][1]
        return {"close": float(v["close"]), "time_label": v["datetime"]}
    except (KeyError, TypeError, ValueError):
        return None


def build_pin_bar_setup_note(pd_, recent_30m, point_size=0.01):
    """
    Finds whichever fib level, across BOTH pin zones, current price is sitting
    closest to right now - restricted to the 50/61.8/78.6% levels (the ones
    actually shown to the reader), so the setup note never references a level
    the reader can't see in the price data box. Precomputed here rather than
    left to the model, since the price-vs-level comparison must be exact.

    Once the nearest level is found, the most recently closed 30-minute
    candle's close relative to that level determines the read:
    - UPPER pin zone (yesterday's rejected rally / resistance wick): closing
      BELOW the level = rejection (favors a SELL); closing ABOVE = break-through
      (favors upside continuation).
    - LOWER pin zone (yesterday's rejected sell-off / support wick): closing
      ABOVE the level = bounce (favors a BUY); closing BELOW = break-through
      (favors downside continuation).

    Distance is reported in both dollars and "points" (point_size, default
    $0.01 - MT4's standard tick size for a 2-digit-quoted XAUUSD symbol; edit
    the point_size default here if your broker quotes gold with different
    digit precision).
    """
    candidates = []
    for label, value in pd_["upper_pin_fib"]:
        if label in DISPLAY_FIB_LEVELS:
            candidates.append(("UPPER", label, value))
    for label, value in pd_["lower_pin_fib"]:
        if label in DISPLAY_FIB_LEVELS:
            candidates.append(("LOWER", label, value))

    current_price = pd_["current_price"]
    zone, level_label, level_price = min(candidates, key=lambda c: abs(current_price - c[2]))
    distance = abs(current_price - level_price)
    distance_points = distance / point_size

    upper_vals = dict(pd_["upper_pin_fib"])
    lower_vals = dict(pd_["lower_pin_fib"])
    zone_range = abs(upper_vals["100%"] - upper_vals["0%"]) if zone == "UPPER" \
        else abs(lower_vals["100%"] - lower_vals["0%"])
    threshold = 0.12 * zone_range  # tighter than a single-level check, since levels sit closer together

    if distance > threshold:
        return (
            f"Current price (${current_price:,.2f}) is not tightly against any specific pin-bar "
            f"level right now. Closest is the {zone} pin zone's {level_label} level "
            f"(${level_price:,.2f}), ${distance:,.2f} away ({distance_points:,.0f} points) - not "
            f"close enough to treat as an active decision point."
        )

    zone_desc = "UPPER pin zone (yesterday's rejected rally / resistance wick)" if zone == "UPPER" \
        else "LOWER pin zone (yesterday's rejected sell-off / support wick)"

    lines = [
        f"Current price (${current_price:,.2f}) is sitting right at the {level_label} level of the "
        f"{zone_desc}, at ${level_price:,.2f} ({distance_points:,.0f} points away) - this is a live "
        f"decision point (reversal vs. breakout)."
    ]

    close_price = recent_30m["close"] if recent_30m else None
    close_time = recent_30m["time_label"] if recent_30m else None

    if close_price is None:
        lines.append(
            "30-minute candle data was unavailable this run, so rejection vs. break-through cannot "
            "be confirmed - flag this as an unconfirmed setup rather than asserting a direction."
        )
        return "\n".join(lines)

    if zone == "UPPER":
        if close_price < level_price:
            lines.append(
                f"The most recently closed 30-minute candle ({close_time}) closed at ${close_price:,.2f}, "
                f"BELOW this level - price is being rejected here. Favors a SELL / fade of the level."
            )
        else:
            lines.append(
                f"The most recently closed 30-minute candle ({close_time}) closed at ${close_price:,.2f}, "
                f"ABOVE this level - price is pushing through it. Favors continuation UPSIDE (breakout), "
                f"not a sell."
            )
    else:
        if close_price > level_price:
            lines.append(
                f"The most recently closed 30-minute candle ({close_time}) closed at ${close_price:,.2f}, "
                f"ABOVE this level - price is bouncing off it. Favors a BUY / bounce."
            )
        else:
            lines.append(
                f"The most recently closed 30-minute candle ({close_time}) closed at ${close_price:,.2f}, "
                f"BELOW this level - price is breaking through it. Favors continuation DOWNSIDE, not a buy."
            )

    return "\n".join(lines)


def maybe_send_email(subject, plain_text, html_body):
    """Sends via Gmail SMTP if EMAIL_USER/EMAIL_PASS secrets are set; otherwise skips
    quietly, since publishing to GitHub Pages is enough on its own."""
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    to_addr = os.environ.get("EMAIL_TO", user)
    if not user or not password:
        print("EMAIL_USER/EMAIL_PASS not set - skipping email, published to GitHub Pages only.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print("Email sent to", to_addr)
