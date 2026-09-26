"""
Shared helpers for the Gold & Macro Monitor scripts.
Mirrors the logic from the Google Apps Script version, ported to Python.
"""
import datetime
import html
import json
import os
import re
import smtplib
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def fetch_news(query, max_items=6):
    """Google News RSS search - free, no key needed. Shared by both the daily
    (macro headlines) and weekly (trend-finding) reports."""
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en"
    resp = requests.get(url, timeout=20)
    root = ET.fromstring(resp.content)
    items = root.findall("./channel/item")[:max_items]
    lines = [f"- {item.findtext('title')} ({item.findtext('pubDate')})" for item in items]
    return "\n".join(lines)

STATE_DIR = "state"
LAST_ANALYSIS_PATH = os.path.join(STATE_DIR, "last_daily_analysis.json")
LAST_WEEKLY_ANALYSIS_PATH = os.path.join(STATE_DIR, "last_weekly_analysis.json")
CALENDAR_WATCH_STATE_PATH = os.path.join(STATE_DIR, "calendar_watch_state.json")
YESTERDAY_PIN_ZONES_PATH = os.path.join(STATE_DIR, "yesterday_pin_zones.json")
PRICE_ALERT_STATE_PATH = os.path.join(STATE_DIR, "price_alert_state.json")
LAST_NETS_PATH = os.path.join(STATE_DIR, "last_nets.json")
LAST_INVALIDATION_PATH = os.path.join(STATE_DIR, "last_invalidation.json")

# Prefixes ask_gemini() returns on failure (see that function) - checked against
# the start of its return value to distinguish a genuine analysis from an error
# string, so a failed call never gets treated as real content and published.
LLM_ERROR_PREFIXES = (
    "[Gemini error:", "[Gemini request failed:", "[Could not parse Gemini response:",
    "[Groq error:", "[Groq request failed:", "[Could not parse Groq response:",
)


def is_gemini_error(text):
    """True if ask_gemini()/ask_groq()/ask_llm()'s return value is an error message, not real analysis."""
    return isinstance(text, str) and text.strip().startswith(LLM_ERROR_PREFIXES)


def _load_json_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_json_state(path, data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as e:
        print(f"Could not save state to {path}: {e}")


def load_last_analysis():
    """
    Reads yesterday's stored daily report for continuity AND as a fallback if
    today's Gemini call fails, since GitHub Actions runs are stateless
    otherwise. Returns a dict with at least {"date", "analysis"} - plus
    whatever extra report context (price_block, setup_note, calendar_block,
    page_timestamp) was saved alongside it, so a failed day can fully replay
    the last successful report rather than just its text - or None if not
    found/unreadable (e.g. first-ever run).
    """
    return _load_json_state(LAST_ANALYSIS_PATH)


def save_last_analysis(date_str, analysis_text, extra=None):
    """
    Persists today's report (analysis text plus optional extra context like
    price_block/setup_note/calendar_block/page_timestamp) so tomorrow's run
    can reference it for continuity, or fall back to it entirely if its own
    Gemini call fails. Rolling single-day memory - overwrites the previous
    entry. Only call this after a GENUINE successful analysis; a fallback run
    that reused cached content must not overwrite this with its own copy, or
    future continuity messaging would lose track of the true last-fresh day.
    """
    data = {"date": date_str, "analysis": analysis_text}
    if extra:
        data.update(extra)
    _save_json_state(LAST_ANALYSIS_PATH, data)


def load_last_weekly_analysis():
    """Weekly equivalent of load_last_analysis() - see that docstring."""
    return _load_json_state(LAST_WEEKLY_ANALYSIS_PATH)


def save_last_weekly_analysis(date_str, analysis_text, extra=None):
    """Weekly equivalent of save_last_analysis() - see that docstring."""
    data = {"date": date_str, "analysis": analysis_text}
    if extra:
        data.update(extra)
    _save_json_state(LAST_WEEKLY_ANALYSIS_PATH, data)


def load_last_nets():
    """Returns {"date":, "nets": [...]} from the last GENUINE daily run, or None."""
    return _load_json_state(LAST_NETS_PATH)


def save_last_nets(nets, date_str):
    """
    Persists the three per-section "Net implication: BULLISH|BEARISH|NEUTRAL"
    values extracted from today's fresh analysis (see extract_net_implications),
    so tomorrow's prompt can hold the model accountable for any flip via
    build_nets_consistency_block(). Only call this with a genuine fresh set of
    exactly 3 - a partial or cached run should not overwrite this.
    """
    _save_json_state(LAST_NETS_PATH, {"date": date_str, "nets": nets})


def build_nets_consistency_block():
    """
    Builds the NET IMPLICATIONS CONSISTENCY prompt block from yesterday's
    saved nets (sections 1/2/3: WHAT CHANGED / REAL YIELD / GEOPOLITICAL OIL
    TRANSMISSION), or "" if none is saved yet (first run). This is what makes
    the "you MUST explain what changed if a net flips" instruction in the
    style guide's COHERENCE PROTOCOL enforceable - without a concrete prior
    value to compare against, the model has nothing to be held accountable
    to and can silently re-derive a different set of three every day.
    """
    prev = load_last_nets()
    if not prev or len(prev.get("nets", [])) < 3:
        return ""
    n = prev["nets"]
    return (
        f"\n\nPRIOR RUN'S SECTION-LEVEL NET IMPLICATIONS (from {prev['date']}) - you MUST "
        "compare your own three net implications against these. If any of yours differs from "
        "the corresponding prior value, that section's Net implication line explaining the "
        "specific new data point that flipped it is not optional - state it plainly:\n"
        f"  Section 1 (WHAT CHANGED) was: {n[0]}\n"
        f"  Section 2 (REAL YIELD / RATE LINKAGE) was: {n[1]}\n"
        f"  Section 3 (GEOPOLITICAL OIL TRANSMISSION) was: {n[2]}"
    )


def load_last_invalidation():
    """Returns {"date":, "content": <WHAT WOULD CHANGE MY MIND text>} or None."""
    return _load_json_state(LAST_INVALIDATION_PATH)


def save_last_invalidation(content, date_str):
    """
    Persists today's fresh "WHAT WOULD CHANGE MY MIND" section text so
    tomorrow's run can anchor to it (see build_invalidation_consistency_block)
    instead of freely re-inventing a different set of invalidation triggers
    every day, which the style guide explicitly warns against.
    """
    _save_json_state(LAST_INVALIDATION_PATH, {"date": date_str, "content": content})


def build_invalidation_consistency_block():
    """
    Builds the INVALIDATION CONSISTENCY prompt block from yesterday's saved
    "WHAT WOULD CHANGE MY MIND" text, or "" if none is saved yet.
    """
    prev = load_last_invalidation()
    if not prev or not prev.get("content"):
        return ""
    return (
        f"\n\nINVALIDATION CONSISTENCY BLOCK (yesterday's WHAT WOULD CHANGE MY MIND, from "
        f"{prev['date']}) - use this as the starting point for today's section 6 anchors. Only "
        "change one of the four anchors if the input data genuinely no longer supports it, and "
        "say explicitly what changed if you do - do not silently swap in a different set of "
        f"triggers:\n{prev['content']}"
    )

def load_calendar_watch_state():
    """Reads the set of event keys already alerted on by the calendar watcher,
    so re-runs every 5 minutes don't re-notify for the same event repeatedly."""
    try:
        with open(CALENDAR_WATCH_STATE_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def save_calendar_watch_state(alerted_keys):
    """Persists the updated set of already-alerted event keys."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(CALENDAR_WATCH_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(alerted_keys), f)
    except OSError as e:
        print(f"Could not save calendar watch state: {e}")


def save_yesterday_pin_zones(pd_):
    """
    Caches yesterday's daily OHLC (used to define the full 0%-100% wick range
    of both pin zones) once per day, written by daily_brief.py right after it
    computes this anyway. This lets the frequent price-alert watcher check
    "is price inside a pin zone" on every 5-minute poll WITHOUT its own daily
    time-series API call each time - that data doesn't change intraday, so
    fetching it 288 times a day would be pure waste (biquote.io's free tier
    is generous, but there's no reason to hammer it for data that's already
    sitting in state from this morning's daily brief run).
    """
    _save_json_state(YESTERDAY_PIN_ZONES_PATH, {
        "yesterday_date": pd_["yesterday_date"],
        "open": pd_["open"], "high": pd_["high"], "low": pd_["low"], "close": pd_["close"],
        "body_top": pd_["body_top"], "body_bottom": pd_["body_bottom"],
    })


def load_yesterday_pin_zones():
    """Reads the cached pin-zone range saved by save_yesterday_pin_zones(), or
    None if not yet available (e.g. the daily brief hasn't run yet today)."""
    return _load_json_state(YESTERDAY_PIN_ZONES_PATH)


def load_price_alert_state():
    """Reads the price-alert watcher's dedup state: which direction was last
    alerted on, and for which reference 30-minute candle, so a breakout that
    persists across multiple 5-minute checks only alerts once."""
    return _load_json_state(PRICE_ALERT_STATE_PATH) or {}


def save_price_alert_state(state):
    """Persists the price-alert watcher's dedup state."""
    _save_json_state(PRICE_ALERT_STATE_PATH, state)


def build_quick_calendar_alert(event):
    """
    Builds a short, immediate Telegram alert for one newly-confirmed economic
    event - a bare beat/miss/inline fact check, not an AI-interpreted read.
    This is the fast layer; the once-a-day brief still does the actual macro
    interpretation (real yield linkage, positioning, etc).
    """
    arrow = ""
    try:
        actual_num = float(str(event["actual"]).replace("%", "").replace("K", "").replace(",", ""))
        forecast_num = float(str(event["forecast"]).replace("%", "").replace("K", "").replace(",", ""))
        if actual_num > forecast_num:
            arrow = "\U0001F53A"  # beat
        elif actual_num < forecast_num:
            arrow = "\U0001F53B"  # miss
        else:
            arrow = "\u2192"  # in line
    except (ValueError, TypeError):
        pass  # non-numeric forecast/actual (e.g. "n/a") - just omit the arrow

    return (
        f"\u26A1 <b>{escape_html(event['title'])}</b> ({escape_html(event['country'])}) {arrow}\n"
        f"Actual: <b>{escape_html(event['actual'])}</b> | Forecast: {escape_html(event['forecast'])} | "
        f"Previous: {escape_html(event['previous'])}\n"
        f"Released: {escape_html(event['time_label'])}"
    )


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
    "they mechanically move US yields on their own.\n"
    "Note: oil prices and geopolitical tension are handled by the separate GEOPOLITICAL OIL "
    "TRANSMISSION framework below, not here - do not re-derive that chain from scratch."
)

GEOPOLITICAL_OIL_FRAMEWORK = (
    "GEOPOLITICAL OIL TRANSMISSION - INTERNAL REASONING (do not show this reasoning in the output):\n"
    "Before writing the brief, silently work through the full transmission chain to arrive at your "
    "conclusion. Do not surface any of these steps in the output - the output only carries the final "
    "synthesis (below).\n\n"
    "LAYER 1 - SHOCK CLASSIFICATION (internal only): Classify the geopolitical oil shock as supply "
    "vs demand, realized vs threatened, which chokepoint is affected (Hormuz ~20% of global supply, "
    "Red Sea ~12%, Russian infrastructure ~8%, Saudi East-West pipeline ~4%), and whether it is "
    "transient or structural. Supply shocks are stagflationary; demand shocks are disinflationary. "
    "Threat-only events usually produce a spike-and-fade, not a sustained shift.\n\n"
    "LAYER 2 - OIL PRICE TRANSLATION (internal only): Estimate the geopolitical risk premium embedded "
    "in the current price using a rough ~$4/bbl per 1 million bpd disrupted heuristic. Check whether "
    "the Brent-WTI spread is widening (confirms seaborne supply risk) or narrowing (risk being "
    "absorbed). Note short-run demand elasticity is approximately -0.03, so prices must move violently "
    "to clear even small supply gaps.\n\n"
    "LAYER 3 - YIELD TRANSMISSION (internal only): Connect oil to breakevens and real yields. A "
    "supply shock that raises long-term inflation expectations faster than nominal yields lowers "
    "real yields (gold-positive). If nominals stay sticky while oil falls, real yields rise "
    "(gold-negative). The oil/yield divergence is the key signal.\n\n"
    "LAYER 4 - FED REACTION FUNCTION (used in output synthesis): The Fed historically does NOT hike "
    "on supply shocks - it holds and \"looks through\" them. The bar for a hike is long-term inflation "
    "expectations de-anchoring. If recession risk rises, the Fed tilts to cuts. Determine a concrete "
    "expectation: hold, hike, or cut, and over what horizon.\n\n"
    "LAYER 5 - SYNTHESIS (used in output synthesis): Combine into a single net gold bias from the oil "
    "chain: BULLISH (supply shock -> breakevens up -> real yields down -> Fed holds/cuts), BEARISH "
    "(demand shock -> oil down -> nominals sticky -> real yields up -> Fed hawkish; or supply shock "
    "forcing a hike), or NEUTRAL (threat-only spike that fades). State the confidence level and the "
    "single biggest reason.\n\n"
    "OUTPUT REQUIREMENT: In the GEOPOLITICAL OIL TRANSMISSION section of the brief, write EXACTLY "
    "TWO SENTENCES synthesizing LAYER 4 and LAYER 5 only. Do NOT mention \"layers\", \"Layer 1-5\", or "
    "any internal reasoning steps. Do NOT include the historical chain, chokepoint enumeration, "
    "elasticity discussion, or premium estimation - those are background. The two sentences must "
    "state (1) the Fed reaction expectation and (2) the net gold bias from the oil chain with "
    "confidence and the biggest reason. If OIL PRICE DATA below says unavailable, reason only "
    "from the news headlines and say the oil price itself is unavailable this run - never invent "
    "a number."
)

ANALYSIS_STYLE_GUIDE = (
    "Write like an experienced macro/futures analyst briefing a trader who already "
    "understands the market - not like a news summary. Do not just restate the inputs "
    "back as a list. Structure your response in SIX short sections with these exact "
    "headers:\n"
    "1. WHAT CHANGED - the one or two things that actually matter from the input, and why. "
    "This includes the economic calendar: using the ECONOMIC CALENDAR DATA given below, which has "
    "three possible buckets - (a) events with a confirmed actual figure: quote the exact Actual "
    "figure, and the Forecast/Previous figures for comparison, directly from that block (do not "
    "paraphrase, round, or approximate it), and state whether it beat, met, or missed forecast and "
    "what that implies; (b) events whose scheduled time has passed but this feed has no actual "
    "figure yet: these DID happen, so treat them as released, not upcoming - check the TARGETED "
    "HEADLINE SEARCH FOR MISSING ACTUALS block below (if present) for the real figure, otherwise say "
    "the outcome is not yet confirmed rather than inventing one; (c) events still ahead today: only "
    "mention these briefly as context for what to watch, do not invent a figure for them. If the "
    "calendar data starts with a NOTE saying no events are scheduled today, treat the listed events "
    "as recent context the market is still digesting. If compared against YESTERDAY'S ANALYSIS given "
    "below nothing meaningful changed, say so explicitly rather than padding. If no prior analysis is "
    "available (first run), just assess today's inputs directly. END THIS SECTION WITH A SINGLE LINE "
    "in this exact format: \"Net implication: BULLISH|BEARISH|NEUTRAL for gold - [one-line reason].\"\n"
    "2. REAL YIELD / RATE LINKAGE - reason through how this connects to US real yields "
    "(nominal rates minus inflation expectations), since that is the dominant driver of gold. "
    "If OIL PRICE DATA is provided below, apply the GEOPOLITICAL OIL TRANSMISSION CHAIN "
    "internally: classify the shock, translate it into the oil price, connect it to "
    "breakevens and real yields, and determine the Fed reaction. Do not treat oil as a "
    "standalone signal disconnected from real yields. END THIS SECTION WITH A SINGLE LINE "
    "in this exact format: \"Net implication: BULLISH|BEARISH|NEUTRAL for gold - [one-line reason].\"\n"
    "3. GEOPOLITICAL OIL TRANSMISSION - this is a dedicated section, but it must be SHORT. "
    "Write EXACTLY TWO SENTENCES. Do NOT show the analytical chain, do NOT mention layers "
    "(no \"Layer 1\", \"Layer 2\", etc.), do NOT include chokepoint enumeration, elasticity math, "
    "premium estimation, or historical context. The FIRST sentence must state the Fed reaction "
    "expectation (hold, hike, or cut, and over what horizon). The SECOND sentence must be "
    "formatted exactly as: \"Net implication: BULLISH|BEARISH|NEUTRAL for gold - [one-line "
    "reason].\"\n"
    "4. DIRECTIONAL VIEW - this section starts with ONE combined summary line, followed by "
    "THREE labelled sub-parts. The summary line comes first, on its own single line, in this "
    "exact format:\n"
    "   **Fundamental backdrop:** VALUE1 || **Extreme Pivots:** VALUE2\n"
    "where VALUE1 is BULLISH, BEARISH, or NEUTRAL and VALUE2 is WAIT, BUY, or SELL (choose one "
    "for each). Bold the two labels with ** as shown, but leave the two values as plain text - "
    "do NOT wrap the values in asterisks and do NOT add a period after either value. This is "
    "ONE single line - do NOT break it into two lines.\n"
    "Then the THREE sub-parts, each with its own bold subheading starting its own paragraph:\n"
    "   **Extreme Pivot (intraday to 3 days):** This sub-part must contain EXACTLY TWO paragraphs, "
    "each on its own line, each labelled as described below.\n"
    "     PARAGRAPH 1 - ACTIVE PIVOT (no label): Address the pivot nearest to the current "
    "price. State whether price is inside a green zone. If it is inside, state the "
    "confirmation/rejection verdict and give a concrete direction (BUY bounce, SELL fade, or "
    "breakout continuation) with the entry level and first target. If it is outside both green "
    "zones, say WAIT and state the exact pivot price to watch for entry - the pivot price must "
    "be quoted verbatim from the PIN-BAR LEVEL SETUP CHECK block below. That block also tells you "
    "which side of each pivot the current price is on and what role that pivot is playing right "
    "now (support vs resistance) - honour that classification exactly. Keep this to two or three "
    "sentences.\n"
    "     PARAGRAPH 2 - SECOND PIVOT: Start this paragraph with the literal bold label "
    "\"**Second Pivot to watch:**\" followed by the far (opposite) pin zone description, its 50% "
    "level price, and the exact distance in POINTS from the current price. CRITICAL: both the "
    "50% price and the point distance must be copied VERBATIM from the PIN-BAR LEVEL SETUP CHECK "
    "block below. Do NOT recompute the 50% by averaging high and low, do NOT compute the point "
    "distance yourself, and do NOT round differently - the values there are already derived from "
    "yesterday's actual open/high/low/close via the pin-bar Fibonacci table. Also state the role "
    "that pivot is currently playing (support vs resistance), which is also given in the setup "
    "check. Then add one short guidance sentence such as \"Keep this on your radar in case today's "
    "move extends that far.\"\n"
    "   **Swing View (1-2 weeks):** State the broader FUNDAMENTAL directional lean for gold - "
    "bullish, bearish, or neutral-range - with a confidence level (low/medium/high) and the "
    "single biggest reason. You MUST state the Combined fundamental arithmetic in one line here "
    "in the form: \"Combined fundamental lean: [Section 1 net] + [Section 2 net] + [Section 3 "
    "net] = [overall].\" Then state the single biggest reason, which MUST name which of the "
    "three earlier macro sections produced the dominant implication. Refer to the sections by "
    "their TOPIC, not by number - say \"driven by the oil-price impact on inflation "
    "expectations\" or \"driven by the real-yield channel\", NEVER \"driven by section 3\" or "
    "\"section 2\". The reader never sees the section numbering. This must be consistent with "
    "the Fundamental backdrop stated above. If the Fundamental backdrop is NEUTRAL but you "
    "take a directional stance here, name the single overriding factor and justify why it "
    "dominates the arithmetic. PRICE LANGUAGE RULE FOR THIS SUB-PART: whenever you reference "
    "a target, a support, an invalidation, or a breakout trigger, you MUST include the concrete "
    "price in parentheses right after the description. NEVER use abstract language such as "
    "\"higher levels\", \"lower levels\", \"resistance\", or \"support\" on their own without the "
    "exact price attached - the reader does not have the Fibonacci table in front of them. Every "
    "actionable line must state a dollar value. Keep this to three or four sentences.\n"
    "   **Reconciliation:** In one sentence, explicitly reconcile the Extreme Pivot and the Swing "
    "View. If they agree, say so in one line. If they conflict, state the alignment condition "
    "(e.g. \"counter-trend bounce inside a bearish swing\") and how the trader should weight the "
    "two horizons. If the reconciliation references a level that would trigger or invalidate "
    "either view, that level MUST be stated as a concrete price. NEVER write phrases like \"if "
    "price breaks above the lower-pin 50% level\" or \"if support holds\" without the accompanying "
    "dollar value - the reader must be able to place an order from the sentence alone. Never "
    "leave the reader to reconcile the calls themselves.\n"
    "   ROLE-FLIP RULE (mandatory, applies to Extreme Pivot, Swing View, and Reconciliation): "
    "before describing what a level does, check which side of it the CURRENT price sits on. A "
    "pivot ABOVE current price acts as RESISTANCE. A pivot BELOW current price acts as SUPPORT. "
    "This overrides the level's historical role: if price has traded BELOW the lower-pin 50% "
    "level, that level is now RESISTANCE, not support - a rally back up to it is the test to "
    "describe, not a fall toward it. If price has traded ABOVE the upper-pin 50% level, that "
    "level is now SUPPORT, not resistance. The PIN-BAR LEVEL SETUP CHECK below tells you the "
    "current side and role for each pivot - use those exact classifications. When writing the "
    "Reconciliation, describe the CURRENT relationship between price and the pivots - do NOT "
    "describe a hypothetical move toward a level price has already reached or passed.\n"
    "5. WHY NOT THE OPPOSITE CASE - state the strongest argument for the opposite direction "
    "(e.g. if you lean bearish, give the honest bull case) and then explain specifically why "
    "the current data does not make that the higher-probability outcome right now. If the data "
    "shows a retracement or pullback, explicitly address whether that looks like a genuine trend "
    "reversal or a normal corrective move within a larger trend, and justify which one using the "
    "specific numbers given - do not just assert \"it is just a pullback\" without reasoning.\n"
    "6. WHAT WOULD CHANGE MY MIND - the specific data point or event that would actually flip the "
    "view. IMPORTANT: this section must be ANCHORED and STABLE across runs, not freely re-invented. "
    "Use these anchor categories in this exact order, one sentence each, and quote concrete "
    "numbers where the input data gives you one:\n"
    "  (a) A price-level break: state the specific XAU/USD level from the PIN-BAR SETUP that would "
    "invalidate the current view. Use the exact Fibonacci prices given to you, not invented ones.\n"
    "  (b) A rates/yields break: state the specific move in US real yields or 2Y nominal yields "
    "that would flip the view. Do not reference a level that was not given to you in the input.\n"
    "  (c) An inflation/oil break: state the specific oil or breakeven move that would flip the "
    "view. Only cite the current oil price that was provided to you - do not invent a new one.\n"
    "  (d) A Fed/BOJ policy event: name the specific event type that would flip the view.\n"
    "Do NOT invent levels, thresholds, or events that were not derivable from the input data "
    "provided to you. Do NOT produce a different set of triggers just because the news headlines "
    "shifted slightly - the anchors above should stay roughly stable from run to run. If an "
    "INVALIDATION CONSISTENCY block is given below, use it as the starting point for these anchors "
    "and only change one if the input data genuinely no longer supports it - explain what changed "
    "if you do.\n\n"
    "COHERENCE PROTOCOL (mandatory - apply before writing):\n"
    "The three macro sections you write each produce a net directional implication for gold. "
    "When you refer to any of them in the body text, name them by topic (the news/"
    "what-changed channel, the real-yield channel, the oil-chain channel) - never by their "
    "internal section number, because the reader does not see the numbering. Each section must "
    "end with its Net implication line as specified above. The three net implications then "
    "combine into a single Fundamental backdrop word that opens section 4. The Extreme Pivot "
    "sub-part fires as the technical setup dictates and must NOT be softened to fit the "
    "fundamentals. When the Extreme Pivot and the fundamental lean disagree, the Reconciliation "
    "sub-part must state that explicitly as a counter-trend condition and tell the trader "
    "which horizon to weight. Do not reverse the technical signal to match the fundamentals. "
    "Do not reverse the fundamentals to match the technicals. State both, state the "
    "reconciliation, and let the trader decide with full information. If a NET IMPLICATIONS "
    "CONSISTENCY block is given below, you MUST explain in that section's own Net implication "
    "line the specific new data point that flipped it, whenever a net differs from the prior run.\n\n"
    "Keep the whole thing under 650 words. Be decisive but honest about uncertainty - do not "
    "hedge every sentence, but do not overstate confidence either. This is analysis to inform "
    "a decision, not investment advice, and you can note that briefly at the end.\n\n"
    "CRITICAL PRICE RULE: only reference the exact current price and Fibonacci levels given to "
    "you explicitly in the CURRENT PRICE DATA section below, and the exact oil price/percentage "
    "change given in OIL PRICE DATA if provided - never invent, round differently, or state any "
    "other specific price level for either instrument. If either section says its data is "
    "unavailable, do not state any specific price or price range for that instrument at all - "
    "describe direction only in relative terms (e.g. 'further downside pressure from current levels').\n\n"
    "CONCRETE-LEVEL RULE (applies to every section, not just Directional View): the reader of "
    "this brief does NOT have the Fibonacci table in front of them. Whenever you refer to a "
    "target, a support, an invalidation, a breakout trigger, or any other price level, you "
    "MUST follow the description with the concrete price in parentheses. Every actionable "
    "sentence must be executable from the numbers alone, without the reader needing to look "
    "anything up. If you cannot name the specific price, do not name the level at all.\n\n"
    "AFTER completing all six sections above, if a TARGETED HEADLINE SEARCH FOR MISSING ACTUALS "
    "block was provided, append one line per event listed in it, in EXACTLY this format so the "
    "figure can be captured programmatically for the archive:\n"
    "EXTRACTED_ACTUAL: <exact event title as given> = <value>\n"
    "Only extract a value if a headline clearly and specifically states the actual reported "
    "figure for that exact event. If uncertain, ambiguous, or no headline mentions a figure, "
    "write UNKNOWN as the value - do not guess a plausible-sounding number. These lines are for "
    "data capture only and will not be shown to the reader."
)

WEEKLY_ANALYSIS_STYLE_GUIDE = (
    "Write like an experienced macro/futures analyst briefing a trader on the week's positioning "
    "picture - not a news summary. Do not just restate the inputs back as a list. Structure your "
    "response in SEVEN short sections with these exact headers:\n"
    "1. WHAT CHANGED - the one or two things that actually matter from this week's COT report "
    "and/or economic data, and why. END THIS SECTION WITH A SINGLE LINE in this exact format: "
    "\"Net implication: BULLISH|BEARISH|NEUTRAL for gold - [one-line reason].\"\n"
    "2. REAL YIELD / RATE LINKAGE - reason through how this week's data connects to US real yields "
    "(nominal rates minus inflation expectations), since that is the dominant driver of gold. If "
    "OIL PRICE DATA is provided below, apply the GEOPOLITICAL OIL TRANSMISSION CHAIN internally "
    "rather than treating oil as a standalone signal. END THIS SECTION WITH A SINGLE LINE in this "
    "exact format: \"Net implication: BULLISH|BEARISH|NEUTRAL for gold - [one-line reason].\"\n"
    "3. GEOPOLITICAL OIL TRANSMISSION - a dedicated section, but SHORT. Write EXACTLY TWO "
    "SENTENCES. Do NOT show the analytical chain or mention layers. The FIRST sentence states the "
    "Fed reaction expectation (hold, hike, or cut, and over what horizon). The SECOND sentence is "
    "formatted exactly as: \"Net implication: BULLISH|BEARISH|NEUTRAL for gold - [one-line "
    "reason].\"\n"
    "4. DIRECTIONAL VIEW - give a clear lean (bullish / bearish / neutral-range) for gold heading "
    "into next week, with a rough confidence level (low/medium/high) and the single biggest reason "
    "for that lean, synthesizing the COT positioning read and this week's data releases together. "
    "This should be broadly consistent with the three Net implication lines above - if it isn't, "
    "say explicitly what overrides them and why. There is no pin-bar or intraday setup data for a "
    "weekly report - do not reference one.\n"
    "5. WHY NOT THE OPPOSITE CASE - state the strongest argument for the opposite direction (e.g. "
    "if you lean bearish, give the honest bull case) and explain specifically why the current data "
    "does not make that the higher-probability outcome right now.\n"
    "6. ECONOMIC CALENDAR & POSITIONING - using the WEEKLY ECONOMIC CALENDAR DATA given below, "
    "summarize the highest-impact USD/JPY releases from the PAST week (not today specifically - "
    "this report looks back over the whole week). Two possible per-event cases: (a) a confirmed "
    "actual figure - state whether it beat, met, or missed forecast and what that implies for "
    "rate-hike odds, the dollar, real yields, and gold - don't just restate the numbers, interpret "
    "them; (b) the data marks an event as occurred but with no actual figure confirmed from any "
    "source - if a TARGETED HEADLINE SEARCH FOR MISSING ACTUALS block is given below, check it for "
    "the real figure and use it if a headline clearly states one; otherwise still mention that the "
    "event occurred and say its outcome isn't confirmed, rather than omitting it or inventing a "
    "number. Do NOT report zero releases for the week just because some figures are unconfirmed - "
    "an occurred-but-unconfirmed event is still real context, not something to skip. Then use the "
    "TREND-FINDING NEWS SEARCH block to place this week's numbers into a MULTI-WEEK narrative - is "
    "this week's data a continuation of a trend that has been building for months, an acceleration, "
    "or a break from it - and explain specifically how that trend context should shape how the COT "
    "positioning read in section 1 gets interpreted (e.g. a hawkish trend that's been building for "
    "months makes fresh long liquidation look more like the start of a real shift than a one-week "
    "blip, or vice versa). Treat the calendar data and the trend search as one connected argument, "
    "not two separate topics. If the calendar data says unavailable or empty, say so explicitly "
    "rather than inventing a release or a trend.\n"
    "7. WHAT WOULD CHANGE MY MIND - the specific data point, COT shift, or event next week that "
    "would actually flip the view.\n"
    "Keep the whole thing under 550 words. Be decisive but honest about uncertainty - do not hedge "
    "every sentence, but do not overstate confidence either. This is analysis to inform a decision, "
    "not investment advice, and you can note that briefly at the end.\n\n"
    "CRITICAL PRICE RULE: only reference the exact oil price/percentage change given in OIL PRICE "
    "DATA if provided - never invent one. If that section says unavailable, do not state any "
    "specific oil price at all.\n\n"
    "AFTER completing all seven sections above, if a TARGETED HEADLINE SEARCH FOR MISSING ACTUALS "
    "block was provided, append one line per event listed in it, in EXACTLY this format so the "
    "figure can be captured programmatically for the archive:\n"
    "EXTRACTED_ACTUAL: <exact event title as given> = <value>\n"
    "Only extract a value if a headline clearly and specifically states the actual reported figure "
    "for that exact event. If uncertain, ambiguous, or no headline mentions a figure, write UNKNOWN "
    "as the value - do not guess a plausible-sounding number. These lines are for data capture only "
    "and will not be shown to the reader."
)

SECTION_HEADERS = [
    # Number prefixes are matched loosely (\d+ not a literal "1") because the
    # daily and weekly guides now number these differently (weekly has an
    # extra ECONOMIC CALENDAR & POSITIONING section daily doesn't have) - the
    # header TEXT is unique enough to match unambiguously either way.
    ("changed", r"\**\s*\d+\.\s*WHAT CHANGED\s*\**:?"),
    ("yield", r"\**\s*\d+\.\s*REAL YIELD.*?LINKAGE\s*\**:?"),
    ("geopolitical", r"\**\s*\d+\.\s*GEOPOLITICAL OIL TRANSMISSION\s*\**:?"),
    ("direction", r"\**\s*\d+\.\s*DIRECTIONAL VIEW\s*\**:?"),
    ("opposite", r"\**\s*\d+\.\s*WHY NOT THE OPPOSITE CASE\s*\**:?"),
    ("calendar", r"\**\s*\d+\.\s*ECONOMIC CALENDAR[^\n]*POSITIONING\s*\**:?"),
    ("mind", r"\**\s*\d+\.\s*WHAT WOULD CHANGE MY MIND\s*\**:?"),
]

SECTION_META = {
    "changed": {"label": "What Changed", "icon": "\U0001F4CC", "color": "#34495e"},
    "yield": {"label": "Real Yield / Rate Linkage", "icon": "\U0001F4B5", "color": "#2980b9"},
    "geopolitical": {"label": "Geopolitical Oil Transmission", "icon": "\U0001F30D", "color": "#c0392b"},
    "opposite": {"label": "Why Not The Opposite Case", "icon": "\U0001F50E", "color": "#8e44ad"},
    "calendar": {"label": "Economic Calendar & Positioning", "icon": "\U0001F4C5", "color": "#16a085"},
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


def ask_groq(prompt, max_retries=2):
    """
    Fallback LLM call via Groq's free, no-credit-card, OpenAI-compatible API -
    tried only when Gemini fails. Free tier: 14,400 requests/day, 30/min, no
    card needed - comfortably enough for occasional fallback use. Requires a
    GROQ_API_KEY secret; returns an error string (never raises) if it's not
    set or the call fails, matching ask_gemini()'s error-string convention so
    callers can check with is_gemini_error() either way.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "[Groq error: GROQ_API_KEY not set]"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}
    last_error = None
    for attempt in range(1, max_retries + 1):
        data = None
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            last_error = f"[Groq request failed: {e}]"

        if data is not None:
            if "error" not in data:
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    last_error = f"[Could not parse Groq response: {str(data)[:500]}]"
            else:
                last_error = f"[Groq error: {data['error'].get('message')}]"

        if attempt < max_retries:
            time.sleep(5)
    return last_error


def ask_llm(prompt):
    """
    Tries Gemini first, falling back to Groq - a different free provider,
    likely to fail independently - if Gemini errors out (e.g. a daily quota
    hit on one provider doesn't mean both are unavailable at once). Returns
    the original Gemini error if both fail, so the caller's single
    is_gemini_error() check still works either way, and the cache-fallback
    logic downstream only kicks in once genuinely no fresh analysis was
    possible from either provider.
    """
    result = ask_gemini(prompt)
    if not is_gemini_error(result):
        return result
    print(f"Gemini failed ({result[:150]}) - trying Groq fallback...")
    groq_result = ask_groq(prompt)
    if not is_gemini_error(groq_result):
        print("Groq fallback succeeded.")
        return groq_result
    print(f"Groq fallback also failed ({groq_result[:150]}).")
    return result


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


SECTION_REF_RE = re.compile(r"\bsection\s+\d+\b", re.IGNORECASE)


def strip_section_refs(text):
    """
    Safety-net cleanup applied to the raw model output before parsing: the
    style guide explicitly forbids referring to "section 3" etc in the body
    text (the reader never sees the section numbering, and daily/weekly now
    number sections differently anyway), but models don't always follow an
    instruction like that with 100% consistency. Rather than leave a stray
    "as discussed in section 2" in a published report, this strips any
    leftover "section N" phrase outright - the surrounding sentence should
    still read fine without it since the instruction already asks for
    topic-based references ("the real-yield channel") alongside it.
    """
    return SECTION_REF_RE.sub("", text)


NET_IMPLICATION_RE = re.compile(r"Net implication:\s*(BULLISH|BEARISH|NEUTRAL)\b", re.IGNORECASE)


def extract_net_implications(analysis_text):
    """
    Pulls every "Net implication: BULLISH|BEARISH|NEUTRAL" line the model
    wrote (one is required at the end of each of sections 1-3 per the style
    guide) in the order they appear. Used to (a) log a quick same-run sanity
    check and (b) persist for the next run's NET IMPLICATIONS CONSISTENCY
    block, which asks the model to justify any flip rather than silently
    re-deriving a different set of three each day.
    """
    return [m.upper() for m in NET_IMPLICATION_RE.findall(analysis_text)]


def direction_color(text):
    # "arrow" is a plain Unicode triangle character (not a colored emoji), so it
    # renders in whatever CSS color the surrounding span sets - this matters
    # because emoji like the "up/down-pointing triangle" pictographs are
    # hard-coded RED in the Unicode spec regardless of direction, which is
    # exactly why an earlier version never actually showed a green buy arrow.
    # "telegram_icon" is a separate, genuinely green/red emoji for Telegram,
    # which can't apply inline CSS color at all.
    if re.search(r"bearish", text, re.IGNORECASE):
        return {"color": "#c0392b", "bg": "#fdecea", "label": "SELL", "arrow": "\u25BC", "telegram_icon": "\U0001F4C9"}
    if re.search(r"bullish", text, re.IGNORECASE):
        return {"color": "#1e8449", "bg": "#eafaf1", "label": "BUY", "arrow": "\u25B2", "telegram_icon": "\U0001F4C8"}
    return {"color": "#b7791f", "bg": "#fef9e7", "label": "NEUTRAL / WAIT", "arrow": "\u25B6", "telegram_icon": "\u27A1\uFE0F"}


FB_LINE_RE = re.compile(r"\**\s*Fundamental backdrop\s*\**:?\s*([^\n]+)", re.IGNORECASE)
PIVOT_LINE_RE = re.compile(r"\**\s*Extreme Pivots?\s*\**:?\s*([^\n]+)", re.IGNORECASE)


def extract_direction_summary(content):
    """
    The daily report's DIRECTIONAL VIEW section opens with a single combined
    line: "**Fundamental backdrop:** X || **Extreme Pivots:** Y" (see
    ANALYSIS_STYLE_GUIDE section 4). This pulls X and Y out separately and
    returns the section content with that line removed, so the two values
    can be rendered as their own quick-take badges instead of duplicated
    inside the body text. Weekly reports (and any daily report where the
    model didn't follow the format) simply won't match either pattern, so
    both return values come back "" and the caller falls back to the older
    single bullish/bearish/neutral scan via direction_color().
    """
    fb_match = FB_LINE_RE.search(content)
    pivot_match = PIVOT_LINE_RE.search(content)
    fb_value = ""
    if fb_match:
        fb_value = fb_match.group(1).replace("**", "").split("||")[0].strip().rstrip(". ").strip()
    pivot_value = ""
    if pivot_match:
        pivot_value = pivot_match.group(1).replace("**", "").split("||")[-1].strip().rstrip(". ").strip()
    stripped = FB_LINE_RE.sub("", content, count=1)
    stripped = PIVOT_LINE_RE.sub("", stripped, count=1)
    stripped = stripped.lstrip("\n").strip()
    return fb_value, pivot_value, stripped


def pivot_badge(pivot_value):
    """Colors the quick-take pill from the Extreme Pivots value (WAIT/BUY/SELL)
    rather than scanning for bullish/bearish text - that word describes the
    Fundamental backdrop, a different value, and would mislabel the pill."""
    v = pivot_value.upper()
    if "SELL" in v:
        return {"color": "#c0392b", "bg": "#fdecea", "label": "SELL", "arrow": "\u25BC", "telegram_icon": "\U0001F4C9"}
    if "BUY" in v:
        return {"color": "#1e8449", "bg": "#eafaf1", "label": "BUY", "arrow": "\u25B2", "telegram_icon": "\U0001F4C8"}
    return {"color": "#b7791f", "bg": "#fef9e7", "label": "WAIT", "arrow": "\u25B6", "telegram_icon": "\u27A1\uFE0F"}


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


def build_nav_pills(nav_links):
    """Shared nav-pill markup, used by build_newsletter_html and by pages built
    outside it (like the calendar archive page), so navigation looks identical
    everywhere instead of drifting between hand-copied versions."""
    if not nav_links:
        return ""
    pills = "".join(
        f'<a href="{escape_html(url)}" style="display:inline-block;padding:6px 14px;margin:0 6px;'
        f'border-radius:16px;background:#f0f0f5;color:#1a1a2e;font-size:12px;font-weight:600;'
        f'text-decoration:none;">{escape_html(label)}</a>'
        for label, url in nav_links
    )
    return f'<div style="text-align:center;padding:14px 0 0;">{pills}</div>'


EXTRACTED_ACTUAL_RE = re.compile(r"^EXTRACTED_ACTUAL:\s*(.+?)\s*=\s*(.+)$", re.MULTILINE)


def extract_and_strip_actuals(analysis_text):
    """
    Pulls out any "EXTRACTED_ACTUAL: <title> = <value>" lines the model
    appended (per the style guide's trailing instruction), returning the
    cleaned display text (with those technical lines removed, since readers
    should never see them) and a dict of {title: value} for any event where
    a real value - not UNKNOWN - was found.
    """
    extracted = {}
    for title, value in EXTRACTED_ACTUAL_RE.findall(analysis_text):
        value = value.strip()
        if value and value.upper() != "UNKNOWN":
            extracted[title.strip()] = value
    cleaned = EXTRACTED_ACTUAL_RE.sub("", analysis_text).strip()
    return cleaned, extracted


def apply_extracted_actuals(events, extracted_actuals):
    """
    Backfills any released_no_actual event whose title matches a figure the
    model extracted from targeted news headlines. Matches on title only
    (case-insensitive) since this is only ever applied to the same day's
    events that were just searched for - marks the source as "news" so
    downstream display/archiving can show it's not an officially-confirmed
    feed figure, just a well-sourced news report of one.
    """
    if not events or not extracted_actuals:
        return events
    lowered = {k.lower(): v for k, v in extracted_actuals.items()}
    for e in events:
        if e["status"] != "released_no_actual":
            continue
        match = lowered.get(e["title"].lower())
        if match:
            e["actual"] = match
            e["status"] = "released"
            e["actual_source"] = "news"
    return events


def build_newsletter_html(title, subtitle, sections, raw_fallback, extra_html_before="", nav_links=None, warning_banner=None):
    order = ["changed", "yield", "geopolitical", "direction", "opposite", "calendar", "mind"]
    body = extra_html_before
    quick_take = ""
    direction_content_for_body = sections.get("direction", "")
    direction_badge = None  # computed once below, reused for both the quick-take pill and the body's colored box

    if not sections:
        body += f'<div style="font-family:{FONT_STACK};font-size:14px;line-height:1.6;">{text_to_html(raw_fallback)}</div>'
    else:
        if "direction" in sections:
            fb_value, pivot_value, stripped = extract_direction_summary(sections["direction"])
            if fb_value or pivot_value:
                # Two-horizon daily format: a colored pill from the actionable
                # Extreme Pivots value, plus both values spelled out below it.
                direction_content_for_body = stripped
                direction_badge = pivot_badge(pivot_value)
                parts = []
                if fb_value:
                    parts.append(f'<strong>Fundamental backdrop:</strong> {escape_html(fb_value)}')
                if pivot_value:
                    parts.append(f'<strong>Extreme Pivots:</strong> {escape_html(pivot_value)}')
                quick_take = (
                    '<div style="text-align:center;margin:0 0 14px 0;">'
                    f'<span style="display:inline-block;padding:7px 18px;border-radius:20px;background:{direction_badge["color"]};'
                    f'color:#fff;font-size:12px;font-weight:bold;letter-spacing:0.8px;">{direction_badge["arrow"]} {direction_badge["label"]}</span>'
                    '</div>'
                    '<div style="max-width:520px;margin:0 auto 20px auto;text-align:center;font-size:14px;'
                    'color:#333;line-height:1.6;">'
                    + ' &nbsp;<span style="color:#bbb;">||</span>&nbsp; '.join(parts) +
                    '</div>'
                    '<hr style="border:none;border-top:1px solid #eee;margin:0 0 18px 0;">'
                )
            else:
                # Older single-value format (weekly reports, or a daily run
                # that didn't follow the two-value line): fall back to the
                # original bullish/bearish/neutral text scan.
                direction_badge = direction_color(sections["direction"])
                first_sentence = re.split(r"[.!?]", sections["direction"])[0].strip()
                quick_take = (
                    '<div style="text-align:center;margin:0 0 20px 0;">'
                    f'<span style="display:inline-block;padding:10px 24px;border-radius:24px;background:{direction_badge["color"]};'
                    f'color:#fff;font-size:16px;font-weight:800;letter-spacing:1px;">{direction_badge["arrow"]} {direction_badge["label"]}</span>'
                    f'<div style="font-size:14px;color:#555;margin-top:10px;font-style:italic;max-width:480px;'
                    f'margin-left:auto;margin-right:auto;">{escape_html(first_sentence)}.</div></div>'
                    '<hr style="border:none;border-top:1px solid #eee;margin:0 0 18px 0;">'
                )
        for key in order:
            if key not in sections:
                continue
            content_html = text_to_html(direction_content_for_body if key == "direction" else sections[key])
            if key == "direction":
                c = direction_badge
                body += (
                    f'<div style="margin:0 0 18px 0;border-radius:10px;background:{c["bg"]};'
                    f'border-left:5px solid {c["color"]};overflow:hidden;">'
                    f'<div style="padding:14px 18px 10px;">'
                    f'<span style="font-size:26px;vertical-align:middle;margin-right:8px;color:{c["color"]};">{c["arrow"]}</span>'
                    f'<span style="font-size:17px;font-weight:800;letter-spacing:0.5px;vertical-align:middle;'
                    f'color:{c["color"]};">{c["label"]}</span></div>'
                    f'<div style="padding:0 18px 16px;font-size:14px;line-height:1.6;">{content_html}</div></div>'
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

    nav_html = build_nav_pills(nav_links)

    banner_html = ""
    if warning_banner:
        banner_html = (
            '<div style="margin:16px 24px 0;padding:14px 16px;border-radius:8px;background:#fff3cd;'
            'border:1px solid #ffe69c;color:#664d03;font-size:13px;line-height:1.5;">'
            f'\u26A0\uFE0F {escape_html(warning_banner)}</div>'
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
  {nav_html}
  {banner_html}
  <div style="border:1px solid #eee;border-top:none;padding:22px 24px 20px;border-radius:0 0 12px 12px;">
    {quick_take}{body}
  </div>
  <div style="color:#999;font-size:11px;margin-top:14px;text-align:center;line-height:1.5;">
    Automatically generated &middot; Not investment advice
  </div>
</div>
</body></html>"""


GOLD_SYMBOL = "XAUUSD"  # biquote.io's no-slash forex-convention ticker for spot gold


def _biquote_request(path, params=None):
    """
    Calls the free biquote.io market-data API - a proper, documented REST
    provider, no API key, no signup, 15,000 requests/min per IP. Replaced
    Twelve Data (which needed a key and a much tighter 800-call/day free
    quota) as the source for gold's current price, previous-day OHLC, and
    the 30-minute/5-minute candles the price watcher needs.

    `path` is the full path under the biquote host, e.g. "/api/XAUUSD" or
    "/api/XAUUSD/ohlc". Returns the parsed JSON body, or None on any
    request/parse/HTTP-error failure (biquote returns error details as JSON
    even on 4xx/5xx, so we still try to log data.get("message") when we can).
    """
    url = f"https://biquote.io{path}"
    try:
        resp = requests.get(url, params=params or {}, timeout=20)
    except requests.RequestException as e:
        print(f"biquote request to {path} failed: {e}")
        return None
    try:
        data = resp.json()
    except ValueError as e:
        print(f"biquote request to {path} returned unparseable JSON: {e}")
        return None
    if resp.status_code >= 400:
        msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)[:200]
        print(f"biquote API error on {path} ({resp.status_code}): {msg}")
        return None
    return data


def _biquote_ohlc(symbol, interval, limit):
    """
    Fetches OHLC bars for `symbol` at `interval` (biquote's own timeframe
    strings: "1m","5m","15m","30m","1h","4h","1d") via biquote's /ohlc
    endpoint, newest-first. Returns the raw "bars" list (each bar carries an
    explicit isOpen flag - see _latest_closed_bar), or None on failure.
    """
    data = _biquote_request(f"/api/{symbol}/ohlc", {"interval": interval, "limit": limit})
    if not data or "bars" not in data:
        return None
    return data["bars"]


def _latest_closed_bar(bars):
    """
    biquote's OHLC bars come back newest-first with the still-forming bar
    explicitly marked isOpen: true - so unlike Twelve Data (where "the
    forming bar is index 0" was an assumption we once got burned by), here
    we can just skip forward past any bar biquote itself says is still
    open, rather than trusting a fixed array position. Returns the first
    closed bar, or None if bars is empty/every bar is somehow open.
    """
    for b in bars:
        if not b.get("isOpen"):
            return b
    return None


def fetch_gold_price_data():
    """
    Fetches current gold spot price and yesterday's completed daily OHLC
    from biquote.io's XAUUSD symbol, then computes Fibonacci levels the way
    the MT4 EA's Pin Zone logic does (FIB_PIN_BOTH): two separate sets
    across each wick - UPPER (candle body top -> day's high) and LOWER
    (candle body bottom -> day's low). This mirrors Fib_DrawPinZones() in
    the EA, applied to the most recent completed daily candle ("yesterday's
    pin bar").

    Returns a dict of structured values, or None if the fetch/parse fails.
    """
    tick = _biquote_request(f"/api/{GOLD_SYMBOL}")
    if not tick or "mid" not in tick:
        return None
    try:
        current_price = float(tick["mid"])
    except (TypeError, ValueError):
        return None

    bars = _biquote_ohlc(GOLD_SYMBOL, "1d", 10)
    if not bars:
        return None

    candles = []
    for v in bars:
        try:
            candles.append({
                "date": v["openTime"][:10],
                "open": float(v["open"]), "high": float(v["high"]),
                "low": float(v["low"]), "close": float(v["close"]),
                "is_open": bool(v.get("isOpen", False)),
            })
        except (KeyError, TypeError, ValueError):
            continue

    if not candles:
        return None

    # bars are newest-first; drop the still-forming bar using biquote's own
    # isOpen flag (no more guessing by comparing dates against "today" the
    # way the old Twelve Data check had to), then walk forward past any
    # degenerate/flat bars (high == low) to find the last genuinely complete
    # daily candle.
    usable = [c for c in candles if not c["is_open"]] or candles

    y = None
    for candidate in usable:
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


OIL_SYMBOL = "USOIL"  # biquote.io's WTI crude ticker
OIL_CHANGE_LOOKBACK_DAYS = 5


def fetch_oil_price_data():
    """
    Fetches current WTI crude price and its %-change over the last
    OIL_CHANGE_LOOKBACK_DAYS closed trading days from biquote.io, feeding the
    GEOPOLITICAL OIL TRANSMISSION chain (see GEOPOLITICAL_OIL_FRAMEWORK) in
    both the daily and weekly prompts. Same pattern as fetch_gold_price_data:
    a tick for the live price, a short daily OHLC window for the lookback
    close, both via biquote so this needs no separate provider/key.

    Returns {"current_price", "past_date", "past_price", "pct_change",
    "lookback_days"} or None if the fetch/parse fails.
    """
    tick = _biquote_request(f"/api/{OIL_SYMBOL}")
    if not tick or "mid" not in tick:
        return None
    try:
        current_price = float(tick["mid"])
    except (TypeError, ValueError):
        return None

    bars = _biquote_ohlc(OIL_SYMBOL, "1d", OIL_CHANGE_LOOKBACK_DAYS + 3)
    if not bars:
        return None

    closes = []
    for v in bars:
        if v.get("isOpen"):
            continue
        try:
            closes.append({"date": v["openTime"][:10], "close": float(v["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < OIL_CHANGE_LOOKBACK_DAYS:
        return None

    # closes is newest-first (closed bars only); the Nth-back trading day is
    # simply index [OIL_CHANGE_LOOKBACK_DAYS - 1].
    past = closes[OIL_CHANGE_LOOKBACK_DAYS - 1]
    pct_change = ((current_price - past["close"]) / past["close"] * 100) if past["close"] else 0.0
    return {
        "current_price": current_price,
        "past_date": past["date"],
        "past_price": past["close"],
        "pct_change": pct_change,
        "lookback_days": OIL_CHANGE_LOOKBACK_DAYS,
    }


def format_oil_context(od):
    """
    Turns fetch_oil_price_data()'s structured dict into the prompt text block
    for OIL PRICE DATA AND GEOPOLITICAL CONTEXT. Degrades gracefully (matching
    format_price_context's convention) so the model reasons qualitatively
    from news headlines alone rather than inventing a number when oil data
    isn't available this run.
    """
    if not od:
        return (
            "Oil price data unavailable this run - do not state any specific oil price, "
            "percentage change, or dollar level for oil. Reason about the geopolitical/oil "
            "transmission chain qualitatively from the news headlines only."
        )
    sign = "+" if od["pct_change"] >= 0 else ""
    return (
        f"WTI crude oil (USOIL): ${od['current_price']:,.2f} "
        f"({sign}{od['pct_change']:.2f}% over the last {od['lookback_days']} trading days, "
        f"vs ${od['past_price']:,.2f} on {od['past_date']}) [source: biquote.io]"
    )


def fetch_recent_30m_close():
    """
    Fetches the most recently CLOSED 30-minute candle for XAU/USD via
    biquote.io - the first bar in its (newest-first) OHLC response that
    isn't flagged isOpen (see _latest_closed_bar), rather than a fixed array
    position. Used to confirm or reject the pin-bar setup below, and to
    anchor stop-loss placement (below the candle's low for a buy, above its
    high for a sell). Returns {"close":, "low":, "high":, "time_label":} or
    None.

    biquote's openTime is always UTC ISO 8601 ("...Z"); this converts it to
    the same DISPLAY_TZ_LABEL convention as the calendar, with the full date
    included - a bare time with no date/timezone label is exactly the kind
    of thing that causes confusion about which day's candle this is.
    """
    bars = _biquote_ohlc(GOLD_SYMBOL, "30m", 5)
    if not bars:
        return None
    bar = _latest_closed_bar(bars)
    if not bar:
        return None
    try:
        dt_utc = datetime.datetime.strptime(bar["openTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        display_tz = datetime.timezone(datetime.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
        dt_display = dt_utc.astimezone(display_tz)
        return {
            "close": float(bar["close"]),
            "low": float(bar["low"]),
            "high": float(bar["high"]),
            "time_label": dt_display.strftime("%b %d, %H:%M") + f" {DISPLAY_TZ_LABEL}",
        }
    except (KeyError, TypeError, ValueError):
        return None


def fetch_latest_5m_open():
    """
    Fetches the newest 5-minute candle's OPEN price for XAU/USD via
    biquote.io, used by the price-alert watcher to check "did the newest
    low-timeframe candle open beyond the previous 30-minute candle's range".

    Prefers the still-FORMING bar (isOpen: true) when biquote flags one -
    unlike Twelve Data (the previous provider here), biquote's OHLC model
    freezes a bar's "open" the instant the bar starts and only lets
    high/low/close move while the bar is in progress, so reading the forming
    bar's open is safe and gives the fastest possible alert - the entire
    point of a 5-minute breakout watcher. Falls back to the newest CLOSED
    bar only if none is flagged open (a defensive edge case, not the normal
    path).

    Returns {"open":, "time_label":} or None.
    """
    bars = _biquote_ohlc(GOLD_SYMBOL, "5m", 3)
    if not bars:
        return None
    bar = bars[0] if bars[0].get("isOpen") else _latest_closed_bar(bars)
    if not bar:
        return None
    try:
        dt_utc = datetime.datetime.strptime(bar["openTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        display_tz = datetime.timezone(datetime.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
        dt_display = dt_utc.astimezone(display_tz)
        return {
            "open": float(bar["open"]),
            "time_label": dt_display.strftime("%b %d, %H:%M") + f" {DISPLAY_TZ_LABEL}",
        }
    except (KeyError, TypeError, ValueError):
        return None


def compute_gold_sma(period=100, interval="5m"):
    """
    Computes a simple moving average of the last `period` CLOSED candle
    closes for XAU/USD, entirely from biquote OHLC data - no separate
    technical-indicator API or key needed. (The reference implementation
    this was ported from called Twelve Data's /sma endpoint specifically for
    this, which would have reintroduced the Twelve Data dependency this
    project deliberately dropped when it switched to biquote.) Used by
    check_price_breakout_alert() as a sanity-checked dynamic stop-loss
    level. Returns the SMA as a float, or None if fewer than `period` closed
    bars are available.
    """
    bars = _biquote_ohlc(GOLD_SYMBOL, interval, period + 2)
    if not bars:
        return None
    closes = []
    for b in bars:
        if b.get("isOpen"):
            continue
        try:
            closes.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if len(closes) >= period:
            break
    if len(closes) < period:
        return None
    return sum(closes) / period


def check_price_breakout_alert():
    """
    The price-alert watcher's core check, run every 5 minutes:

    1. GATE: is the newest 5-minute candle's open price inside EITHER pin
       zone's FULL wick range (0%-100%, not just the narrow green zone) -
       using yesterday's cached OHLC (see save_yesterday_pin_zones), no
       fresh daily-series API call needed for this part.
    2. TRIGGER: does that same open price break above the previous
       30-minute candle's high, or below its low.
    3. STOP-LOSS: prefers the 100-period 5-minute SMA (see compute_gold_sma)
       as the stop-loss level, but only if it actually sits on the
       protective side of BOTH suggested entries below (below both for a
       BUY, above both for a SELL) - an SMA that's drifted to the wrong
       side would suggest a stop that's already been blown through, so
       that's a sign to distrust it for this alert and fall back to the
       reference 30-minute candle's opposite extreme instead.
    4. ENTRIES: two suggested entries - a 61.8% retracement of the previous
       30-minute candle's range back from the breakout side (a pullback
       entry), and the breakout level itself (that candle's own high/low).
    5. DEDUP: tracks alerted_buy/alerted_sell independently per reference
       30-minute candle, rather than a single "last alert" key - a single
       key can't represent both booleans at once, so a BUY firing and then
       (rarely) a SELL firing for the SAME reference candle would overwrite
       the stored key and let the BUY silently re-fire on the next poll. Two
       explicit flags avoid that.

    Returns an alert message string if a NEW breakout is detected, or None
    (either nothing happened, or it already fired for this exact breakout).
    """
    cached_zones = load_yesterday_pin_zones()
    if not cached_zones:
        print("No cached pin-zone data yet (daily brief hasn't run today) - skipping check.")
        return None

    candle_5m = fetch_latest_5m_open()
    candle_30m = fetch_recent_30m_close()
    if not candle_5m or not candle_30m:
        print("Price data unavailable this check (5m or 30m fetch failed) - skipping.")
        return None

    open_price = candle_5m["open"]
    body_top, body_bottom = cached_zones["body_top"], cached_zones["body_bottom"]
    day_high, day_low = cached_zones["high"], cached_zones["low"]

    in_upper_wick = body_top <= open_price <= day_high
    in_lower_wick = day_low <= open_price <= body_bottom
    if not (in_upper_wick or in_lower_wick):
        return None  # outside both full pin-bar wick ranges - not gated, no alert

    prev_high, prev_low = candle_30m["high"], candle_30m["low"]
    direction = None
    if open_price > prev_high:
        direction = "BUY"
    elif open_price < prev_low:
        direction = "SELL"
    if direction is None:
        return None  # inside the previous 30-min range - no breakout

    # Dedup: independent alerted_buy/alerted_sell flags per reference 30-min candle.
    state = load_price_alert_state()
    bar_key = candle_30m["time_label"]
    if state.get("bar_key") != bar_key:
        state = {"bar_key": bar_key, "alerted_buy": False, "alerted_sell": False}
    flag = "alerted_buy" if direction == "BUY" else "alerted_sell"
    if state.get(flag):
        return None
    state[flag] = True
    save_price_alert_state(state)

    zone_name = "UPPER" if in_upper_wick else "LOWER"

    prev_range = prev_high - prev_low
    fib_618 = prev_range * 0.618
    entry1 = (prev_high - fib_618) if direction == "BUY" else (prev_low + fib_618)
    entry2 = prev_high if direction == "BUY" else prev_low

    sma100 = compute_gold_sma()
    sl_source = "100 SMA"
    stop_loss = None
    if sma100 is not None:
        valid_for_buy = direction == "BUY" and sma100 < entry1 and sma100 < entry2
        valid_for_sell = direction == "SELL" and sma100 > entry1 and sma100 > entry2
        if valid_for_buy or valid_for_sell:
            stop_loss = sma100
        else:
            print(
                f"Breakout alert: 100 SMA at ${sma100:,.2f} is on the wrong side of the "
                f"entries for a {direction} - falling back to reference-bar extreme."
            )
    if stop_loss is None:
        stop_loss = prev_low if direction == "BUY" else prev_high
        sl_source = "reference-bar extreme (100 SMA unavailable or on the wrong side)"

    arrow = "\U0001F4C8" if direction == "BUY" else "\U0001F4C9"
    sl_desc = "below" if direction == "BUY" else "above"
    entry_icon = "\U0001F7E2" if direction == "BUY" else "\U0001F534"
    entry_label = "buy limit" if direction == "BUY" else "sell limit"

    print(
        f"Breakout alert [{sl_source}]: {direction} at ${open_price:,.2f}, entries "
        f"${entry1:,.2f}/${entry2:,.2f}, SL ${stop_loss:,.2f}"
    )

    return (
        f"{arrow} <b>Price Breakout Alert \u2014 {direction}</b>\n"
        f"A 5-minute candle opened at ${open_price:,.2f} ({candle_5m['time_label']}), breaking "
        f"{'above' if direction == 'BUY' else 'below'} the previous 30-minute candle's "
        f"{'high' if direction == 'BUY' else 'low'} (${prev_high if direction == 'BUY' else prev_low:,.2f}, "
        f"{candle_30m['time_label']}), while inside yesterday's {zone_name} pin bar range.\n"
        f"{entry_icon} Suggested 1st entry: ${entry1:,.2f} [{entry_label}]\n"
        f"{entry_icon} Suggested 2nd entry: ${entry2:,.2f}\n"
        f"\u274C Suggested stop-loss: {sl_desc} ${stop_loss:,.2f} [{sl_source}]."
    )


GREEN_ZONE_RADIUS_POINTS = 1000  # +/- radius around each zone's 50% level; edit this one number to tune it


def build_pin_bar_setup_note(pd_, recent_30m, point_size=0.01):
    """
    Two-stage check, both precomputed here rather than left to the model
    since exact price-vs-level comparisons must be reliable:

    STAGE 1 - GREEN ZONE GATE: is current price within GREEN_ZONE_RADIUS_POINTS
    of either pin zone's 50% level? This is a broad "is this even worth
    considering a trade near these levels right now" pre-qualifier - being
    close to 50% (not necessarily exactly on 61.8% or 78.6%) is still a valid
    entry consideration area. If price is outside BOTH zones' green zones,
    the trader is told exactly how far away (in points) they are from the
    nearest zone's edge, and given the specific price (the "pivot") to watch
    for price to re-enter the zone - i.e. wait, don't force a trade out here.

    STAGE 2 - if inside a green zone, the trigger is a breakout relative to
    the most recent closed 30-minute candle's OWN range (not the pin-bar
    level itself, which only gates whether this area is worth watching at
    all):
    - Current price ABOVE that candle's high -> BUY confirmed (breakout).
      Stop-loss suggested below that same candle's low.
    - Current price BELOW that candle's low -> SELL confirmed (breakdown).
      Stop-loss suggested above that same candle's high.
    - Current price still inside that candle's range -> no breakout yet,
      wait rather than assert a direction.

    Every return path also reports the SECOND (opposite) pin zone's 50%
    pivot and its distance from current price, so the trader can prepare
    for price reaching that side too, not just whichever zone is currently
    most relevant.

    Distances are reported in both dollars and "points" (point_size, default
    $0.01 - MT4's standard tick size for a 2-digit-quoted XAUUSD symbol; edit
    the point_size default here if your broker quotes gold with different
    digit precision).
    """
    current_price = pd_["current_price"]
    upper_vals = dict(pd_["upper_pin_fib"])
    lower_vals = dict(pd_["lower_pin_fib"])
    radius = GREEN_ZONE_RADIUS_POINTS * point_size
    zone_mids = {"UPPER": upper_vals["50%"], "LOWER": lower_vals["50%"]}

    def pivot_role(pivot_price):
        """ROLE-FLIP RULE: a pivot above current price acts as resistance, a
        pivot below acts as support - regardless of the zone's historical/
        static label (e.g. price can trade below the "resistance" wick's
        50% level, at which point a rally back up to it is a resistance
        test, not a fall toward support). See ANALYSIS_STYLE_GUIDE section 4."""
        return "resistance" if pivot_price > current_price else "support"

    def pivot_roles_line():
        return (
            f"PIVOT ROLES (per current price - honour these, not a zone's static historical label): "
            f"UPPER pin 50% (${upper_vals['50%']:,.2f}) is currently acting as "
            f"{pivot_role(upper_vals['50%'])}; LOWER pin 50% (${lower_vals['50%']:,.2f}) is "
            f"currently acting as {pivot_role(lower_vals['50%'])}."
        )

    def describe_other_zone(primary_zone_name):
        """Describes the OPPOSITE pin zone's 50% pivot, so the trader can see both
        potential areas at once and prepare for price reaching the other side,
        not just whichever zone happens to be closest right now."""
        other_name = "LOWER" if primary_zone_name == "UPPER" else "UPPER"
        other_mid = zone_mids[other_name]
        distance_points = abs(current_price - other_mid) / point_size
        move_word = "rally" if other_mid > current_price else "fall" if other_mid < current_price else "reach"
        other_desc = "UPPER pin zone (resistance / rejected-rally wick)" if other_name == "UPPER" \
            else "LOWER pin zone (support / rejected-selloff wick)"
        return (
            f"SECOND PIVOT TO WATCH - the {other_desc}'s 50% level sits at ${other_mid:,.2f}; price "
            f"would need to {move_word} roughly {distance_points:,.0f} points to reach it. Keep this "
            f"on your radar in case today's move extends that far."
        )

    zones = [
        ("UPPER", upper_vals["50%"], upper_vals["50%"] - radius, upper_vals["50%"] + radius),
        ("LOWER", lower_vals["50%"], lower_vals["50%"] - radius, lower_vals["50%"] + radius),
    ]

    in_zone = next((name for name, mid, lo, hi in zones if lo <= current_price <= hi), None)

    if in_zone is None:
        # Outside both green zones - find the nearer zone's closest edge (the pivot to watch).
        edge_candidates = []
        for name, mid, lo, hi in zones:
            if current_price < lo:
                edge_candidates.append((name, lo, lo - current_price))
            else:  # current_price > hi
                edge_candidates.append((name, hi, current_price - hi))
        zone_name, pivot_price, distance = min(edge_candidates, key=lambda c: c[2])
        distance_points = distance / point_size
        return (
            f"Current price (${current_price:,.2f}) is OUTSIDE the {GREEN_ZONE_RADIUS_POINTS}-point "
            f"green zone around the {zone_name} pin zone's 50% level - ${distance:,.2f} "
            f"({distance_points:,.0f} points) away from the nearest edge of that zone. "
            f"Advise the trader to WAIT rather than force an entry here. The pivot level to watch "
            f"is ${pivot_price:,.2f} - once price reaches that level (entering the green zone), "
            f"it becomes a valid entry consideration area again.\n\n" + describe_other_zone(zone_name) +
            "\n\n" + pivot_roles_line()
        )

    zone_fib = pd_["upper_pin_fib"] if in_zone == "UPPER" else pd_["lower_pin_fib"]
    level_candidates = [(label, value) for label, value in zone_fib if label in DISPLAY_FIB_LEVELS]
    level_label, level_price = min(level_candidates, key=lambda c: abs(current_price - c[1]))
    distance = abs(current_price - level_price)
    distance_points = distance / point_size

    zone_desc = "UPPER pin zone (yesterday's rejected rally / resistance wick)" if in_zone == "UPPER" \
        else "LOWER pin zone (yesterday's rejected sell-off / support wick)"

    lines = [
        f"Current price (${current_price:,.2f}) is INSIDE the green zone around the {in_zone} pin "
        f"zone's 50% level - a valid entry consideration area. Nearest specific level within it: "
        f"{level_label} of the {zone_desc}, at ${level_price:,.2f} ({distance_points:,.0f} points away)."
    ]

    if recent_30m is None:
        lines.append(
            "30-minute candle data was unavailable this run, so no breakout trigger could be checked "
            "- flag this as an unconfirmed setup rather than asserting a direction."
        )
        lines.append("")
        lines.append(describe_other_zone(in_zone))
        lines.append("")
        lines.append(pivot_roles_line())
        return "\n".join(lines)

    candle_low = recent_30m["low"]
    candle_high = recent_30m["high"]
    candle_time = recent_30m["time_label"]
    lines.append(
        f"Reference 30-minute candle ({candle_time}): High ${candle_high:,.2f} / Low ${candle_low:,.2f}."
    )

    if current_price > candle_high:
        lines.append(
            f"Current price (${current_price:,.2f}) is ABOVE this candle's high - BUY confirmed (breakout)."
        )
        lines.append(f"Suggested stop-loss: below the 30-minute candle's low, at ${candle_low:,.2f}.")
    elif current_price < candle_low:
        lines.append(
            f"Current price (${current_price:,.2f}) is BELOW this candle's low - SELL confirmed (breakdown)."
        )
        lines.append(f"Suggested stop-loss: above the 30-minute candle's high, at ${candle_high:,.2f}.")
    else:
        lines.append(
            f"Current price (${current_price:,.2f}) is still INSIDE this candle's range - no breakout "
            f"confirmed yet. Wait for a clean move above ${candle_high:,.2f} (buy) or below "
            f"${candle_low:,.2f} (sell) before entering; do not assert a direction yet."
        )

    lines.append("")
    lines.append(describe_other_zone(in_zone))
    lines.append("")
    lines.append(pivot_roles_line())
    return "\n".join(lines)


DISPLAY_TZ_OFFSET_HOURS = 3  # GMT+3 - change this one number if you trade from a different timezone
DISPLAY_TZ_LABEL = "GMT+3"


def current_display_timestamp():
    """Returns the current time formatted in DISPLAY_TZ_LABEL, for stamping pages with
    exactly when they were generated - important since daily.html/weekly.html get
    overwritten every run, and a bare date alone can't distinguish between two runs on
    the same day (which came up directly when testing the continuity feature)."""
    display_tz = datetime.timezone(datetime.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
    now_display = datetime.datetime.now(datetime.timezone.utc).astimezone(display_tz)
    return now_display.strftime("%b %d, %Y - %H:%M") + f" {DISPLAY_TZ_LABEL}"


def fetch_raw_calendar_events(currencies=("USD", "JPY"), min_impact="Medium"):
    """
    Fetches this week's economic calendar from Forex Factory's public export
    feed - the same free, key-less JSON feed countless MT4/MT5 news-filter
    EAs use - and returns EVERY matching (currency + impact) event across the
    whole week, with NO date-window restriction applied yet. This makes
    exactly ONE network call.

    IMPORTANT: this function must only be called ONCE per script run. This
    feed is rate-limited by Forex Factory to ~2 requests per 5 minutes per
    IP - a caller that needs several different date views (today, a weekend
    fallback, a multi-day archive window) must fetch once here and then use
    filter_calendar_window()/select_todays_or_recent_from() below to derive
    each view in memory, rather than calling this function again. An earlier
    version called the network 2-3 times per run and got hard-blocked with
    an HTTP 429 as a direct result - that failure mode is exactly what this
    split exists to prevent.

    Each event is classified into one of three statuses using TWO
    independent signals rather than trusting FF's "actual" field alone (that
    field does not reliably populate promptly on this feed):
    - released: actual figure IS populated - the clean, fully-confirmed case.
    - released_no_actual: scheduled time has already passed (10-minute
      buffer) but FF hasn't populated an actual figure yet.
    - upcoming: scheduled time is still in the future.

    Returns a list of event dicts (each with an "event_date" ISO string for
    later filtering), or None if the fetch/parse fails.
    """
    url = f"https://nfs.faireconomy.media/ff_calendar_thisweek.json?nocache={int(time.time())}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"Economic calendar fetch failed: HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        events = resp.json()
        if not isinstance(events, list):
            print(f"Economic calendar returned unexpected format: {str(events)[:200]}")
            return None
    except (requests.RequestException, ValueError) as e:
        print(f"Economic calendar fetch failed: {e}")
        return None

    display_tz = datetime.timezone(datetime.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
    now_display = datetime.datetime.now(datetime.timezone.utc).astimezone(display_tz)
    occurred_buffer = datetime.timedelta(minutes=10)

    impact_rank = {"Low": 0, "Medium": 1, "High": 2}
    min_rank = impact_rank.get(min_impact, 1)

    results = []
    skipped_unparsed = 0
    matched_currency_impact = 0
    for ev in events:
        try:
            country = ev.get("country", "")
            if country not in currencies:
                continue
            impact = ev.get("impact", "Low")
            if impact_rank.get(impact, 0) < min_rank:
                continue
            matched_currency_impact += 1

            raw_date = ev.get("date", "") or ""
            try:
                event_dt = datetime.datetime.fromisoformat(raw_date)
            except ValueError:
                skipped_unparsed += 1
                continue
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=datetime.timezone.utc)
            event_dt_display = event_dt.astimezone(display_tz)

            actual = (ev.get("actual") or "").strip()
            has_actual = bool(actual)
            time_passed = (now_display - event_dt_display) >= occurred_buffer

            if has_actual:
                status = "released"
            elif time_passed:
                status = "released_no_actual"
                # Print the raw fields for this specific event, so if it's still
                # blank after the cache-busting fix, we have hard evidence of
                # exactly what this feed actually contains for it right now,
                # rather than guessing about field names again.
                print(f"released_no_actual diagnostic - raw event data: {ev}")
            else:
                status = "upcoming"

            results.append({
                "title": ev.get("title", "Unknown event"),
                "country": country,
                "event_date": event_dt_display.date().isoformat(),
                "time_label": event_dt_display.strftime("%b %d, %H:%M") + f" {DISPLAY_TZ_LABEL}",
                "impact": impact,
                "forecast": (ev.get("forecast") or "").strip() or "n/a",
                "previous": (ev.get("previous") or "").strip() or "n/a",
                "actual": actual or None,
                "status": status,
            })
        except Exception:
            continue

    print(
        f"Economic calendar: fetched {len(events)} total events this week, "
        f"{matched_currency_impact} matched currency/impact filters, {len(results)} parsed "
        f"successfully ({skipped_unparsed} had unparseable dates)."
    )
    return results


def filter_calendar_window(raw_events, days_back=0):
    """
    Pure in-memory filter (no network call) - narrows an already-fetched
    fetch_raw_calendar_events() list down to events from today back through
    `days_back` prior days (today defined in DISPLAY_TZ_OFFSET_HOURS).

    KNOWN LIMITATION: the raw feed only contains THIS calendar week's events,
    so a days_back window that crosses back into the previous week (e.g.
    filtering on a Monday or Tuesday) will not find those earlier events -
    that data was never fetched in the first place, since it lives in a
    different weekly file. Disclosed tradeoff, not a bug.
    """
    if raw_events is None:
        return None
    display_tz = datetime.timezone(datetime.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(display_tz).date()
    earliest = today - datetime.timedelta(days=days_back)
    filtered = [e for e in raw_events if earliest <= datetime.date.fromisoformat(e["event_date"]) <= today]
    print(f"Economic calendar: {len(filtered)} of {len(raw_events)} events fall within the "
          f"requested {days_back}-day-back window.")
    return filtered


def select_todays_or_recent_from(raw_events, max_lookback=4):
    """
    Pure in-memory selection (no network call) - given an already-fetched
    fetch_raw_calendar_events() list, returns today's events, or - if today
    has none, which is EXPECTED and normal on weekends/holidays, not an
    error - falls back to the most recent day within max_lookback that did
    have events, so a Saturday/Sunday report still carries Friday's releases
    as relevant context instead of a blank "nothing today".

    Returns (events, is_fallback, events_date_label) - see
    fetch_todays_or_recent_calendar's old docstring for the field meanings
    (kept identical for compatibility with existing callers).
    """
    if raw_events is None:
        return None, False, None

    display_tz = datetime.timezone(datetime.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(display_tz).date()

    today_events = [e for e in raw_events if e["event_date"] == today.isoformat()]
    if today_events:
        return today_events, False, today.strftime("%b %d, %Y")

    earliest = today - datetime.timedelta(days=max_lookback)
    wider = [e for e in raw_events if earliest <= datetime.date.fromisoformat(e["event_date"]) <= today]
    if not wider:
        return [], False, None

    most_recent_date = max(e["event_date"] for e in wider)
    recent_events = [e for e in wider if e["event_date"] == most_recent_date]
    recent_label = datetime.datetime.fromisoformat(most_recent_date).strftime("%b %d, %Y")
    return recent_events, True, recent_label


def fetch_economic_calendar(currencies=("USD", "JPY"), min_impact="Medium", days_back=0):
    """
    Convenience wrapper for callers that only need ONE calendar view per run
    (e.g. calendar_watcher.py, which only ever wants today's events). Does a
    single fetch_raw_calendar_events() call plus one filter_calendar_window()
    call. Do NOT use this if you need multiple different date-window views in
    the same run - call fetch_raw_calendar_events() once yourself and derive
    each view with filter_calendar_window()/select_todays_or_recent_from()
    instead, to avoid the multi-call rate-limit problem this split fixes.
    """
    raw = fetch_raw_calendar_events(currencies=currencies, min_impact=min_impact)
    if raw is None:
        return None
    return filter_calendar_window(raw, days_back=days_back)


def fetch_todays_or_recent_calendar(currencies=("USD", "JPY"), min_impact="Medium", max_lookback=4):
    """
    Convenience wrapper for callers that only need this ONE view per run.
    Do NOT use this together with fetch_economic_calendar() or another call
    to this function in the same run - that reintroduces the multi-call
    rate-limit problem. If you need this view AND an archive window in the
    same run, call fetch_raw_calendar_events() once and derive both views
    with select_todays_or_recent_from()/filter_calendar_window() instead.
    """
    raw = fetch_raw_calendar_events(currencies=currencies, min_impact=min_impact)
    return select_todays_or_recent_from(raw, max_lookback=max_lookback)


def format_calendar_context(events, is_fallback=False, events_date_label=None):
    """Turns the list from fetch_todays_or_recent_calendar()/fetch_economic_calendar()
    into the prompt/display text, with three clearly separated buckets, every time
    labeled in DISPLAY_TZ_LABEL, and an explicit note when this is a fallback to the
    most recent trading day rather than genuinely today's events (e.g. weekends)."""
    if events is None:
        return "Economic calendar data unavailable this run - do not invent any scheduled events."
    if not events:
        if is_fallback:
            return (
                f"No Medium/High-impact USD or JPY events found even when looking back several "
                f"days ({DISPLAY_TZ_LABEL})."
            )
        return f"No Medium/High-impact USD or JPY events scheduled for today ({DISPLAY_TZ_LABEL})."

    released = [e for e in events if e["status"] == "released"]
    upcoming = [e for e in events if e["status"] == "upcoming"]

    if is_fallback:
        lines = [
            f"NOTE: No events are scheduled today (likely a weekend or market holiday). Showing "
            f"the most recent trading day's releases instead ({events_date_label}, all times in "
            f"{DISPLAY_TZ_LABEL}) as the relevant recent context - these already happened, they "
            f"are not today's events."
        ]
    else:
        lines = [f"(All times below are in {DISPLAY_TZ_LABEL}.)"]

    day_word = "THAT DAY" if is_fallback else "TODAY"
    if released:
        lines.append("")
        lines.append(f"ALREADY RELEASED {day_word} (actual figure confirmed):")
        for e in released:
            if e.get("actual_source") == "mql5":
                source_note = " (via MQL5 calendar, not the FF feed)"
            elif e.get("actual_source") == "news":
                source_note = " (via news, not the official feed)"
            else:
                source_note = ""
            lines.append(
                f"  - [{e['time_label']}] [{e['country']}, {e['impact']} impact] {e['title']} - "
                f"Actual: {e['actual']}{source_note} | Forecast: {e['forecast']} | Previous: {e['previous']}"
            )
    # Events still stuck at released_no_actual after MQL5 + targeted-news backfill
    # (see fetch_targeted_headlines_for_missing_actuals in daily_brief.py, which runs
    # BEFORE this function and is what promotes most of them into the "released"
    # bucket above) are intentionally not shown here - the reader asked for the
    # confirmed-actuals section only, not a running list of unresolved placeholders.
    if upcoming and not is_fallback:  # a fallback day is entirely in the past - nothing "upcoming" about it
        lines.append("")
        lines.append("NOT YET RELEASED TODAY:")
        for e in upcoming:
            lines.append(
                f"  - [{e['time_label']}] [{e['country']}, {e['impact']} impact] {e['title']} - "
                f"Forecast: {e['forecast']} | Previous: {e['previous']}"
            )
    return "\n".join(lines)


def format_weekly_calendar_summary(raw_events):
    """
    Summarizes a week's worth of already-fetched Medium/High-impact USD/JPY
    calendar events (with any MQL5 backfill AND targeted-headline backfill
    already applied) for the weekly COT report - a flat past-week recap
    sorted chronologically, not the today/upcoming split the daily report
    uses, since by the time the weekly report runs (Saturday) the whole
    week's data has already occurred.

    Includes confirmed-actual events AND any event that's still stuck as
    released_no_actual after both backfill passes - these DID happen (their
    scheduled time has passed), so omitting them entirely would understate
    what the week actually contained. By Saturday, MQL5's live calendar
    scrape can no longer see most of Monday-Friday's events (it only
    reflects near-current dates), so without listing the no-actual ones too,
    a week that genuinely had several releases can end up reporting zero -
    which is exactly the bug this fixed.
    """
    if raw_events is None:
        return "Weekly economic calendar data unavailable this run - do not invent any releases."
    relevant = [e for e in raw_events if e["status"] in ("released", "released_no_actual")]
    if not relevant:
        return "No Medium/High-impact USD or JPY releases occurred this week."

    lines = [f"(All times in {DISPLAY_TZ_LABEL}, sorted by date/time.)"]
    for e in sorted(relevant, key=lambda x: (x["event_date"], x["time_label"])):
        if e["status"] == "released_no_actual":
            lines.append(
                f"  - [{e['time_label']}] [{e['country']}, {e['impact']} impact] {e['title']} - "
                f"THIS EVENT OCCURRED but no actual figure could be confirmed from any source "
                f"this run. Forecast: {e['forecast']} | Previous: {e['previous']}. Mention it as a "
                f"release that happened, without inventing a number for it."
            )
            continue
        source_note = ""
        if e.get("actual_source") == "mql5":
            source_note = " (via MQL5 calendar, not the FF feed)"
        elif e.get("actual_source") == "news":
            source_note = " (via news, not the official feed)"
        lines.append(
            f"  - [{e['time_label']}] [{e['country']}, {e['impact']} impact] {e['title']} - "
            f"Actual: {e['actual']}{source_note} | Forecast: {e['forecast']} | Previous: {e['previous']}"
        )
    return "\n".join(lines)


def save_calendar_archive(date_str, iso_date, events):
    """
    Archives the day's filtered Medium/High-impact calendar events (previous,
    forecast, actual) to docs/archive/ - as raw JSON (structured, for reuse)
    and a simple readable HTML table (for browsing via the Pages site).
    Complements the yesterday's-analysis state file: this preserves the
    underlying data permanently, that captures the AI's read on it for one
    rolling day.
    """
    os.makedirs("docs/archive", exist_ok=True)

    json_path = f"docs/archive/calendar-{iso_date}.json"
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "events": events or []}, f, indent=2)
    except OSError as e:
        print(f"Could not write calendar archive JSON: {e}")

    status_labels = {
        "released": "Released", "released_no_actual": "Released (no actual)", "upcoming": "Upcoming"
    }
    rows = ""
    for e in (events or []):
        status_display = status_labels.get(e.get("status"), e.get("status", ""))
        if e.get("status") == "released" and e.get("actual_source") == "mql5":
            status_display = "Released (via MQL5)"
        elif e.get("status") == "released" and e.get("actual_source") == "news":
            status_display = "Released (via news)"
        rows += (
            "<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('time_label', ''))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('country', ''))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('impact', ''))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('title', ''))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('previous') or 'n/a')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('forecast') or 'n/a')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(e.get('actual') or 'n/a')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{escape_html(status_display)}</td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='8' style='padding:10px;color:#888;'>No Medium/High-impact USD or JPY events that day.</td></tr>"

    nav_html = build_nav_pills([
        ("Daily Brief", "../daily.html"), ("Weekly COT", "../weekly.html"),
        ("Archive Index", "."), ("Home", "../index.html"),
    ])

    html_out = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calendar Archive - {escape_html(date_str)}</title></head>
<body style="margin:0;padding:24px;background:#f2f2f2;font-family:{FONT_STACK};">
<div style="max-width:720px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:20px 24px;">
    <div style="color:#8ab4f8;font-size:11px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;">Gold &amp; Macro Intelligence</div>
    <h1 style="color:#fff;margin:4px 0 0;font-size:19px;">Economic Calendar Archive</h1>
    <div style="color:#a9b4c4;font-size:12px;margin-top:4px;">Rolling window as of {escape_html(date_str)} - each event shows its own date (times in {DISPLAY_TZ_LABEL})</div>
  </div>
  {nav_html}
  <div style="padding:16px 24px 24px;overflow-x:auto;">
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead><tr style="text-align:left;color:#555;">
        <th style="padding:6px 10px;">Time</th><th style="padding:6px 10px;">Ccy</th>
        <th style="padding:6px 10px;">Impact</th><th style="padding:6px 10px;">Event</th>
        <th style="padding:6px 10px;">Previous</th><th style="padding:6px 10px;">Forecast</th>
        <th style="padding:6px 10px;">Actual</th><th style="padding:6px 10px;">Status</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <div style="color:#999;font-size:11px;text-align:center;padding:0 0 16px;">Automatically generated</div>
</div>
</body></html>"""
    html_path = f"docs/archive/calendar-{iso_date}.html"
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_out)
    except OSError as e:
        print(f"Could not write calendar archive HTML: {e}")


def rebuild_archive_index():
    """
    Regenerates docs/archive/index.html listing every archived file.
    GitHub Pages does not provide automatic directory listings, so without
    this file, the "Archive" nav link on every page 404s on that folder -
    this has likely been broken since the nav links were first added.
    Groups by type and sorts each group newest-first (filenames embed ISO
    dates, so a reverse string sort orders them correctly).
    """
    archive_dir = "docs/archive"
    os.makedirs(archive_dir, exist_ok=True)
    try:
        files = sorted(os.listdir(archive_dir), reverse=True)
    except OSError as e:
        print(f"Could not list archive directory: {e}")
        return

    groups = {"Daily Briefs": [], "Weekly COT Analyses": [], "Economic Calendar Data": []}
    for fname in files:
        if fname in ("index.html", ".gitkeep"):
            continue
        if fname.startswith("daily-") and fname.endswith(".html"):
            groups["Daily Briefs"].append(fname)
        elif fname.startswith("weekly-") and fname.endswith(".html"):
            groups["Weekly COT Analyses"].append(fname)
        elif fname.startswith("calendar-") and fname.endswith(".html"):
            groups["Economic Calendar Data"].append(fname)
        # calendar-*.json files are intentionally not linked individually -
        # the matching .html page is the browsable entry point for that data.

    sections_html = ""
    for label, flist in groups.items():
        if not flist:
            continue
        items = "".join(
            f'<li style="margin-bottom:6px;"><a href="{escape_html(f)}" style="color:#1a1a2e;">{escape_html(f)}</a></li>'
            for f in flist
        )
        sections_html += (
            f'<h3 style="font-size:14px;color:#34495e;margin:20px 0 8px;">{escape_html(label)}</h3>'
            f'<ul style="list-style:none;padding:0;font-size:13px;">{items}</ul>'
        )
    if not sections_html:
        sections_html = '<p style="color:#888;">No archived reports yet.</p>'

    nav_html = build_nav_pills([("Daily Brief", "../daily.html"), ("Weekly COT", "../weekly.html"), ("Home", "../index.html")])

    html_out = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archive</title></head>
<body style="margin:0;padding:24px;background:#f2f2f2;font-family:{FONT_STACK};">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:20px 24px;">
    <div style="color:#8ab4f8;font-size:11px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;">Gold &amp; Macro Intelligence</div>
    <h1 style="color:#fff;margin:4px 0 0;font-size:19px;">Archive</h1>
  </div>
  {nav_html}
  <div style="padding:16px 24px 24px;">
    {sections_html}
  </div>
</div>
</body></html>"""
    try:
        with open(os.path.join(archive_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html_out)
    except OSError as e:
        print(f"Could not write archive index: {e}")


def build_telegram_digest(title, subtitle, sections):
    """
    Builds a Telegram-safe message containing the Directional View,
    Geopolitical Oil Transmission, and Economic Calendar & Positioning
    sections in full - not a link-out digest. Telegram's HTML parse_mode
    only supports a small tag subset (b, i, u, s, a, code, pre), so styling
    here is limited to bold labels, not the full CSS/card layout of the
    email/web version.
    """
    lines = [f"<b>{escape_html(title)}</b>", escape_html(subtitle), ""]

    if "direction" in sections:
        fb_value, pivot_value, stripped = extract_direction_summary(sections["direction"])
        if fb_value or pivot_value:
            c = pivot_badge(pivot_value)
            label_bits = []
            if fb_value:
                label_bits.append(f"Fundamental: {fb_value}")
            if pivot_value:
                label_bits.append(f"Extreme Pivots: {pivot_value}")
            lines.append(f"{c['telegram_icon']} <b>Directional View \u2014 {' | '.join(label_bits)}</b>")
            lines.append(escape_html(stripped))
        else:
            c = direction_color(sections["direction"])
            lines.append(f"{c['telegram_icon']} <b>Directional View \u2014 {c['label']}</b>")
            lines.append(escape_html(sections["direction"]))
        lines.append("")

    if "geopolitical" in sections:
        lines.append("\U0001F30D <b>Geopolitical Oil Transmission</b>")
        lines.append(escape_html(sections["geopolitical"]))
        lines.append("")

    if "calendar" in sections:
        lines.append("\U0001F4C5 <b>Economic Calendar &amp; Positioning</b>")
        lines.append(escape_html(sections["calendar"]))

    return "\n".join(lines).strip()


def maybe_send_telegram(message_text):
    """
    Sends a message via the Telegram Bot API if TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are set as secrets; otherwise skips quietly (same
    optional-and-safe pattern as maybe_send_email).

    Setup: message @BotFather on Telegram, send /newbot, follow the prompts
    to get a bot token. Then send any message to your new bot, and visit
    https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates in a browser to find
    your chat_id in the response.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - skipping Telegram send.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message_text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=20)
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram send failed: {data}")
        else:
            print("Telegram message sent.")
    except (requests.RequestException, ValueError) as e:
        print(f"Telegram send failed: {e}")


MQL5_DIV_RE = re.compile(r'<div class="ec-table__item ec-table__item_inline">(.*?)</div>', re.DOTALL)
MQL5_TAG_RE = re.compile(r'<[^>]+>')
MQL5_ROW_RE = re.compile(
    r'^(\d{4}\.\d{2}\.\d{2}) (\d{2}:\d{2}), ([A-Z]{3}), ([^,]+?)'
    r'(?:, Actual: ([^,]+))?(?:, Forecast: ([^,]+))?(?:, Previous: ([^,]+))?\s*$'
)


def fetch_mql5_calendar_actuals(currencies=("USD", "JPY")):
    """
    Scrapes MQL5's public economic calendar page for actual/forecast/previous
    figures, as a direct numeric backfill source for actuals Forex Factory's
    feed doesn't reliably provide - preferred over LLM-extracted news-headline
    figures since it's a structured number straight from the source, not an
    inference over freeform text.

    Confirmed working against real page markup: each event is a
    <div class="ec-table__item ec-table__item_inline"> containing plain text
    "YYYY.MM.DD HH:MM, CCY, Title, Actual: X, Forecast: Y, Previous: Z" (any
    of the three value fields may be absent). This is HTML scraping of an
    unofficial page (no documented API), so it's inherently more fragile than
    an API and could silently break if MQL5 changes their markup - the
    diagnostic line below exists specifically so a future break shows up
    immediately as "0 rows parsed" in the log rather than silently returning
    nothing forever.

    Returns a dict {(event_date_iso, title_lower): {"actual":, "forecast":,
    "previous":}}, or None if the page fetch itself fails entirely (an empty
    dict, as opposed to None, means the fetch worked but nothing matched -
    itself a useful diagnostic signal).
    """
    url = "https://www.mql5.com/en/economic-calendar"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"MQL5 calendar fetch failed: HTTP {resp.status_code}")
            return None
        html_text = resp.text
    except requests.RequestException as e:
        print(f"MQL5 calendar fetch failed: {e}")
        return None

    divs = MQL5_DIV_RE.findall(html_text)
    results = {}
    parsed_count = 0
    for raw in divs:
        text = MQL5_TAG_RE.sub("", raw).strip()
        text = text.replace("&amp;", "&").replace("&nbsp;", " ")
        m = MQL5_ROW_RE.match(text)
        if not m:
            continue
        date_str, _time_str, ccy, title, actual, forecast, previous = m.groups()
        if ccy not in currencies:
            continue
        try:
            event_date_iso = datetime.datetime.strptime(date_str, "%Y.%m.%d").date().isoformat()
        except ValueError:
            continue
        parsed_count += 1
        results[(event_date_iso, title.strip().lower())] = {
            "actual": actual.strip() if actual else None,
            "forecast": forecast.strip() if forecast else None,
            "previous": previous.strip() if previous else None,
        }

    print(
        f"MQL5 calendar: found {len(divs)} row div(s) on the page, parsed {parsed_count} "
        f"successfully, {len(results)} matched currencies {currencies}."
    )
    return results


def apply_mql5_actuals(events, mql5_data):
    """
    Backfills any released_no_actual event using MQL5's calendar scrape,
    matching on (event_date, title) case-insensitively. Marks the source as
    "mql5" so downstream display/archiving can distinguish it from an
    officially-confirmed FF feed figure or a news-headline-derived one -
    MQL5 is a direct numeric source, more trustworthy than an LLM's read of
    a news headline, but still not the original FF feed's own confirmation.
    """
    if not events or not mql5_data:
        return events
    for e in events:
        if e["status"] != "released_no_actual":
            continue
        key = (e["event_date"], e["title"].strip().lower())
        match = mql5_data.get(key)
        if match and match.get("actual"):
            e["actual"] = match["actual"]
            e["status"] = "released"
            e["actual_source"] = "mql5"
    return events


COUNTRY_SEARCH_NAMES = {"USD": "US", "JPY": "Japan"}


def fetch_targeted_headlines_for_missing_actuals(calendar_events, max_events=5):
    """
    For any Medium/High-impact event whose actual figure isn't confirmed yet
    (status == "released_no_actual" - its scheduled time has passed but
    neither the FF feed nor an MQL5 backfill populated a number), runs a
    targeted news search specifically for that event's result. Major
    indicators almost always get their actual figure reported in financial
    news headlines within minutes of release - a more reliable channel for
    this specific gap than either calendar feed, which we've confirmed (via
    two separate providers) doesn't populate actuals reliably for every
    event.

    Shared by both daily_brief.py (same-day events) and weekly_cot.py (a
    full week's worth, most of which MQL5's live-calendar scrape can no
    longer see by the time the weekly report runs) - this is the fallback
    that actually recovers those numbers in both cases, not just daily's.
    """
    if not calendar_events:
        return ""

    missing = [e for e in calendar_events if e.get("status") == "released_no_actual"]
    if not missing:
        return ""

    searched = missing[:max_events]
    skipped = missing[max_events:]
    print(
        f"Targeted headline search: {len(missing)} event(s) missing an actual, "
        f"searching {len(searched)} (cap={max_events}), skipping {len(skipped)} due to the cap."
    )
    if skipped:
        print(f"Skipped due to cap: {[e['title'] for e in skipped]}")

    blocks = []
    for e in searched:
        country_name = COUNTRY_SEARCH_NAMES.get(e["country"], e["country"])
        query = f'{country_name} {e["title"]} actual result'
        headlines = fetch_news(query, max_items=4)
        headline_count = len(headlines.splitlines()) if headlines else 0
        print(f'Targeted search "{query}" -> {headline_count} headline(s) found.')
        blocks.append(
            f'Search for "{e["title"]}" ({e["country"]}):\n' +
            (headlines or "  (no relevant headlines found)")
        )
    return "\n\n".join(blocks)


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
