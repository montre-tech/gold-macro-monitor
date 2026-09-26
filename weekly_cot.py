"""
Weekly Gold COT Analysis.
Pulls the last 10 weeks of COMEX Gold Commitments of Traders data from CFTC's public
API, computes trend/percentile context and long-vs-short asymmetry in Python (not left
to the model), then asks Gemini to write the analysis. Publishes to docs/weekly.html,
optionally emails it, and optionally sends a Telegram digest.
"""
import os

import requests

from lib import (
    CARRY_TRADE_FRAMEWORK,
    GEOPOLITICAL_OIL_FRAMEWORK,
    WEEKLY_ANALYSIS_STYLE_GUIDE,
    ask_llm,
    is_gemini_error,
    parse_sections,
    strip_section_refs,
    extract_and_strip_actuals,
    apply_extracted_actuals,
    build_newsletter_html,
    build_telegram_digest,
    maybe_send_telegram,
    escape_html,
    maybe_send_email,
    fetch_news,
    fetch_oil_price_data,
    format_oil_context,
    fetch_raw_calendar_events,
    fetch_mql5_calendar_actuals,
    apply_mql5_actuals,
    fetch_targeted_headlines_for_missing_actuals,
    format_weekly_calendar_summary,
    load_last_weekly_analysis,
    save_last_weekly_analysis,
    rebuild_archive_index,
    current_display_timestamp,
)

# Trend-oriented queries (not single-event queries like the daily report uses) -
# looking for multi-week narrative context (has inflation been trending up for
# months? has Fed tone been building hawkish since some date?) that a single
# week's isolated data points can't provide on their own.
WEEKLY_TREND_QUERIES = [
    "Federal Reserve interest rate trend outlook",
    "US inflation trend forecast months",
    "gold price outlook analysts next week",
    "Bank of Japan policy trend yen",
    "oil price trend inflation outlook",
    "geopolitical risk gold demand trend",
]

COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def fetch_gold_cot_history(weeks=10):
    params = {
        "$where": "market_and_exchange_names='GOLD - COMMODITY EXCHANGE INC.'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": weeks,
    }
    resp = requests.get(COT_URL, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def describe_asymmetry(long_change, short_change, label):
    """Answers 'did the drop come from long liquidation, or from fresh shorting?'
    directly in code, so it is guaranteed correct regardless of model quality."""
    if long_change < 0 and short_change <= 0:
        return (
            f"{label}: the move came from LONG LIQUIDATION (longs cut by {abs(long_change)} "
            f"contracts) while shorts barely changed ({'+' if short_change >= 0 else ''}{short_change}). "
            "This is profit-taking/de-risking, not fresh conviction in the opposite direction."
        )
    if long_change < 0 and short_change > 0:
        return (
            f"{label}: BOTH long liquidation ({long_change}) AND fresh short building (+{short_change}) "
            "occurred together - a stronger, more genuine bearish signal than long liquidation alone."
        )
    if long_change > 0 and short_change > 0:
        return (
            f"{label}: both longs (+{long_change}) and shorts (+{short_change}) increased - "
            "two-sided positioning building, not a clean directional signal either way."
        )
    if long_change > 0 and short_change < 0:
        return (
            f"{label}: longs added (+{long_change}) while shorts covered ({short_change}) - "
            "a genuine bullish shift, not just short-covering noise."
        )
    return f"{label}: longs changed by {long_change}, shorts changed by {short_change}."


def main():
    rows = fetch_gold_cot_history(10)
    if len(rows) < 2:
        raise SystemExit("Fewer than 2 weeks of COT data returned: " + str(rows)[:1000])

    series = []
    for r in reversed(rows):  # oldest -> newest
        nc_long = int(r["noncomm_positions_long_all"])
        nc_short = int(r["noncomm_positions_short_all"])
        comm_long = int(r["comm_positions_long_all"])
        comm_short = int(r["comm_positions_short_all"])
        series.append({
            "date": r["report_date_as_yyyy_mm_dd"][:10],
            "oi": int(r["open_interest_all"]),
            "nc_long": nc_long, "nc_short": nc_short,
            "comm_long": comm_long, "comm_short": comm_short,
            "nc_net": nc_long - nc_short,
            "comm_net": comm_long - comm_short,
        })

    latest, prior = series[-1], series[-2]
    nc_net_values = [s["nc_net"] for s in series]
    window_max, window_min = max(nc_net_values), min(nc_net_values)
    pct_of_range = 50 if window_max == window_min else round(
        (latest["nc_net"] - window_min) / (window_max - window_min) * 100
    )

    recent = series[-4:]
    trend_lines = "\n".join(
        f'{s["date"]}: OI={s["oi"]}, NonComm Net Long={s["nc_net"]}, Commercial Net={s["comm_net"]}'
        for s in recent
    )

    week_change = latest["nc_net"] - prior["nc_net"]
    week_change_pct = "n/a" if prior["nc_net"] == 0 else f'{(week_change / abs(prior["nc_net"])) * 100:.1f}'

    nc_asym = describe_asymmetry(
        latest["nc_long"] - prior["nc_long"], latest["nc_short"] - prior["nc_short"], "Non-Commercial (speculators)"
    )
    comm_asym = describe_asymmetry(
        latest["comm_long"] - prior["comm_long"], latest["comm_short"] - prior["comm_short"], "Commercial (hedgers)"
    )

    data_summary = (
        f"COMEX Gold COT, last 4 weekly reports (oldest to newest):\n{trend_lines}\n\n"
        f"Latest week-over-week change in speculative net long: {week_change} contracts ({week_change_pct}%)\n"
        f"Current net long sits at approximately the {pct_of_range}th percentile of the last {len(series)} "
        "weeks (0 = most bearish extreme in this window, 100 = most bullish extreme).\n"
        f"(Note: the {pct_of_range}th percentile figure already reflects all {len(series)} weeks of history, "
        "not just the 4 shown above.)\n\n"
        "PRECOMPUTED ASYMMETRY (use this directly - it tells you whether the net change is real "
        f"directional conviction or just position-trimming):\n- {nc_asym}\n- {comm_asym}"
    )

    weekly_calendar_events = fetch_raw_calendar_events()
    mql5_actuals = fetch_mql5_calendar_actuals()
    weekly_calendar_events = apply_mql5_actuals(weekly_calendar_events, mql5_actuals)
    # By Saturday, MQL5's live-calendar scrape above can no longer see most of
    # Monday-Friday's events (it only reflects near-current dates), so a full
    # week typically still has several released_no_actual events left even
    # after that backfill - this targeted search is what actually recovers
    # most of them. Capped higher than daily's default (a week has more
    # events than a single day) but still bounded to avoid an unbounded
    # number of news searches on a very busy week.
    weekly_targeted_headlines_block = fetch_targeted_headlines_for_missing_actuals(
        weekly_calendar_events, max_events=15
    )
    weekly_calendar_block = format_weekly_calendar_summary(weekly_calendar_events)

    oil_data = fetch_oil_price_data()
    oil_block = format_oil_context(oil_data)

    trend_news_block = ""
    for q in WEEKLY_TREND_QUERIES:
        trend_news_block += f'\n\nSearch: "{q}"\n{fetch_news(q, max_items=5)}'

    prompt = (
        CARRY_TRADE_FRAMEWORK + "\n\n" + GEOPOLITICAL_OIL_FRAMEWORK + "\n\n" + WEEKLY_ANALYSIS_STYLE_GUIDE +
        "\n\nHere is the CFTC Commitments of Traders trend data for COMEX Gold you need to "
        "analyze. Pay close attention to the precomputed asymmetry note - it tells you whether a "
        "net-position drop reflects genuine reversal risk or just profit-taking:\n\n" + data_summary +
        "\n\nOIL PRICE DATA AND GEOPOLITICAL CONTEXT (you MUST walk through all five layers of "
        "the GEOPOLITICAL OIL TRANSMISSION CHAIN internally and produce only the two-sentence "
        "synthesis):\n" +
        oil_block +
        "\n\nWEEKLY ECONOMIC CALENDAR DATA (confirmed AND occurred-but-unconfirmed releases from "
        "this specific week - see instructions in ECONOMIC CALENDAR & POSITIONING above for how to "
        "use this):\n" +
        weekly_calendar_block +
        (
            "\n\nTARGETED HEADLINE SEARCH FOR MISSING ACTUALS (the calendar data above marked "
            "these events' actual figures as not confirmed by either the calendar feed or the MQL5 "
            "backfill - these are dedicated news searches for each one specifically. If a headline "
            "states the real figure, use it and note it came from news coverage. If none of these "
            "mention a figure either, say the outcome is not confirmed rather than inventing one):\n"
            + weekly_targeted_headlines_block
            if weekly_targeted_headlines_block else ""
        ) +
        "\n\nTREND-FINDING NEWS SEARCH (use this to establish the MULTI-WEEK narrative behind this "
        "week's isolated data points - e.g. has inflation been trending in one direction for several "
        "months, has Fed/BOJ tone been building in a direction, what are analysts saying about the "
        "trajectory rather than just this week's print - and explicitly tie that trend into how it "
        "likely affects the COT positioning read above, not as a separate disconnected topic. Use "
        "the oil/geopolitical headlines specifically to classify the shock in LAYER 1 of the "
        "transmission chain above):" +
        trend_news_block
    )

    analysis_raw = ask_llm(prompt)
    page_subtitle = f"COT data as of {latest['date']} \u00b7 generated {current_display_timestamp()}"
    warning_banner = None

    if is_gemini_error(analysis_raw):
        print(f"Gemini call failed this run: {analysis_raw}")
        cached = load_last_weekly_analysis()
        if cached and cached.get("analysis"):
            analysis = cached["analysis"]
            sections = parse_sections(analysis)
            data_summary = cached.get("data_summary", data_summary)
            weekly_calendar_block = cached.get("weekly_calendar_block", weekly_calendar_block)
            cached_label = cached.get("page_subtitle") or cached.get("date", "an earlier run")
            warning_banner = (
                f"AI analysis service was unavailable this run ({analysis_raw[:120]}). Showing the "
                f"last successful weekly report instead, originally generated {cached_label}. Treat "
                f"all figures below as reflecting that earlier report, not this week's data."
            )
            # Do NOT call save_last_weekly_analysis here - keep the state pointing at
            # the true last-fresh report, not this repeated fallback.
        else:
            print("No cached weekly report available to fall back to - skipping publish entirely this run.")
            return
    else:
        analysis, extracted_actuals = extract_and_strip_actuals(analysis_raw)
        analysis = strip_section_refs(analysis)
        sections = parse_sections(analysis)

        # Backfill any actual figures the model found in news headlines back into
        # the calendar data, same pattern as daily_brief.py - weekly_calendar_events
        # and raw_calendar (used for the archive) share the same dict objects, so
        # this mutation is visible wherever weekly_calendar_events is referenced.
        weekly_calendar_events = apply_extracted_actuals(weekly_calendar_events, extracted_actuals)
        weekly_calendar_block = format_weekly_calendar_summary(weekly_calendar_events)

        save_last_weekly_analysis(latest["date"], analysis, extra={
            "page_subtitle": page_subtitle,
            "data_summary": data_summary,
            "weekly_calendar_block": weekly_calendar_block,
        })

    data_box_html = (
        '<div style="font-family:monospace;font-size:12px;color:#444;background:#f4f4f4;'
        'padding:12px 14px;border-radius:6px;white-space:pre-wrap;margin-bottom:8px;">'
        + escape_html(data_summary) + "</div>"
        '<div style="font-family:monospace;font-size:12px;color:#444;background:#fff8e1;'
        'padding:12px 14px;border-radius:6px;white-space:pre-wrap;margin-bottom:14px;">'
        + escape_html(weekly_calendar_block) + "</div>"
    )
    html_out = build_newsletter_html(
        "Weekly Gold COT Analysis", page_subtitle, sections, analysis, data_box_html,
        nav_links=[("Daily Brief →", "daily.html"), ("Archive", "archive/"), ("Home", "index.html")],
        warning_banner=warning_banner,
    )
    html_out_archived = build_newsletter_html(
        "Weekly Gold COT Analysis", page_subtitle, sections, analysis, data_box_html,
        nav_links=[("Daily Brief →", "../daily.html"), ("Archive", "."), ("Home", "../index.html")],
        warning_banner=warning_banner,
    )

    os.makedirs("docs/archive", exist_ok=True)
    with open("docs/weekly.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    with open(f'docs/archive/weekly-{latest["date"]}.html', "w", encoding="utf-8") as f:
        f.write(html_out_archived)

    rebuild_archive_index()

    email_subject = "Weekly Gold COT Analysis - " + latest["date"]
    telegram_subtitle = page_subtitle
    if warning_banner:
        email_subject = "[CACHED] " + email_subject
        telegram_subtitle = f"\u26A0\uFE0F CACHED - {page_subtitle}"

    maybe_send_email(
        email_subject,
        data_summary + "\n\n---\n\n" + analysis,
        html_out,
    )

    telegram_digest = build_telegram_digest("Weekly Gold COT Analysis", telegram_subtitle, sections)
    maybe_send_telegram(telegram_digest)


if __name__ == "__main__":
    main()
