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
    "biggest reason for that lean.\n"
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


def build_newsletter_html(title, subtitle, sections, raw_fallback, extra_html_before=""):
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
  <div style="border:1px solid #eee;border-top:none;padding:22px 24px 20px;border-radius:0 0 12px 12px;">
    {quick_take}{body}
  </div>
  <div style="color:#999;font-size:11px;margin-top:14px;text-align:center;line-height:1.5;">
    Automatically generated &middot; Not investment advice
  </div>
</div>
</body></html>"""


def fetch_gold_price_context():
    """
    Fetches current gold price and yesterday's completed daily OHLC from Yahoo
    Finance's public chart endpoint (no key needed), then computes Fibonacci
    levels the way the MT4 EA's Pin Zone logic does (FIB_PIN_BOTH) - NOT a
    plain high-to-low retracement, but two separate Fibonacci sets measured
    across each wick: the UPPER wick (candle body top -> day's high) and the
    LOWER wick (candle body bottom -> day's low). This mirrors
    Fib_DrawPinZones() in the EA, applied to the most recent completed daily
    candle ("yesterday's pin bar").

    Caveats, on purpose:
    - Unofficial Yahoo endpoint - if it changes/blocks, this returns None and
      the prompt is told to state no specific price at all.
    - "Yesterday" = Yahoo's daily bar boundary, which may not exactly match
      your broker's session close time in MT4 - a close approximation, not a
      pixel-exact match to your EA's own reading.
    """
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X"
        params = {"interval": "1d", "range": "10d"}
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        data = resp.json()
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        timestamps = result["timestamp"]
        opens, highs, lows, closes = quote["open"], quote["high"], quote["low"], quote["close"]

        candles = []
        for i in range(len(timestamps)):
            if None not in (opens[i], highs[i], lows[i], closes[i]):
                candles.append({
                    "ts": timestamps[i], "open": opens[i], "high": highs[i],
                    "low": lows[i], "close": closes[i],
                })

        if len(candles) < 2:
            return None

        current_price = result.get("meta", {}).get("regularMarketPrice", candles[-1]["close"])
        y = candles[-2]  # last fully completed daily bar before today's in-progress one
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

        upper_pin_fib = fib_set(body_top, y["high"])    # upper wick
        lower_pin_fib = fib_set(body_bottom, y["low"])  # lower wick

        yesterday_date = datetime.datetime.utcfromtimestamp(y["ts"]).strftime("%b %d, %Y")

        lines = [
            f"Current gold price (XAUUSD): ${current_price:,.2f}",
            f"Yesterday's ({yesterday_date}) candle: Open ${y['open']:,.2f} / High ${y['high']:,.2f} "
            f"/ Low ${y['low']:,.2f} / Close ${y['close']:,.2f}",
            f"Yesterday's candle body: top ${body_top:,.2f} / bottom ${body_bottom:,.2f}",
            "",
            "UPPER PIN ZONE Fibonacci (body top -> day's high, the upper wick):",
        ]
        for label, level in upper_pin_fib:
            lines.append(f"  {label}: ${level:,.2f}")
        lines.append("")
        lines.append("LOWER PIN ZONE Fibonacci (body bottom -> day's low, the lower wick):")
        for label, level in lower_pin_fib:
            lines.append(f"  {label}: ${level:,.2f}")

        return "\n".join(lines)
    except Exception as e:
        print(f"Price fetch failed: {e}")
        return None


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
