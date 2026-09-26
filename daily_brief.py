"""
Daily Gold/Macro Brief.
Pulls news headlines on Fed policy, BOJ/yen intervention, and gold, then asks Gemini
to synthesize a structured analysis. Publishes to docs/daily.html (served by GitHub
Pages), optionally emails it if EMAIL_USER/EMAIL_PASS secrets are set, and optionally
sends a Telegram digest if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID secrets are set.
"""
import datetime
import os

from lib import (
    CARRY_TRADE_FRAMEWORK,
    GEOPOLITICAL_OIL_FRAMEWORK,
    ANALYSIS_STYLE_GUIDE,
    ask_llm,
    is_gemini_error,
    parse_sections,
    strip_section_refs,
    extract_net_implications,
    save_last_nets,
    build_nets_consistency_block,
    save_last_invalidation,
    build_invalidation_consistency_block,
    build_newsletter_html,
    build_telegram_digest,
    maybe_send_telegram,
    maybe_send_email,
    fetch_news,
    fetch_gold_price_data,
    format_price_context,
    save_yesterday_pin_zones,
    fetch_recent_30m_close,
    build_pin_bar_setup_note,
    fetch_oil_price_data,
    format_oil_context,
    fetch_raw_calendar_events,
    select_todays_or_recent_from,
    format_calendar_context,
    fetch_mql5_calendar_actuals,
    apply_mql5_actuals,
    fetch_targeted_headlines_for_missing_actuals,
    extract_and_strip_actuals,
    apply_extracted_actuals,
    load_last_analysis,
    save_last_analysis,
    save_calendar_archive,
    rebuild_archive_index,
    current_display_timestamp,
    escape_html,
)

NEWS_QUERIES = [
    "Federal Reserve interest rate decision",
    "Bank of Japan yen intervention",
    "gold price real yields",
    "CME FedWatch rate hike odds",
    "oil price WTI Brent inflation",
    "geopolitical tensions gold safe haven",
]


def main():
    news_block = ""
    for q in NEWS_QUERIES:
        news_block += f'\n\nSearch: "{q}"\n{fetch_news(q)}'

    price_data = fetch_gold_price_data()

    if price_data:
        price_block = format_price_context(price_data)
        recent_30m = fetch_recent_30m_close()
        setup_note = build_pin_bar_setup_note(price_data, recent_30m)
        save_yesterday_pin_zones(price_data)
    else:
        price_block = "Price data unavailable this run - do not state any specific price or price range."
        setup_note = "Setup check unavailable - price data was missing this run."

    # Fetch the raw calendar ONCE for this whole run - both the live view and the
    # archive window get derived from this same list in memory, no repeat network
    # calls. Calling the calendar feed more than once per run risks Forex
    # Factory's ~2-requests-per-5-minutes rate limit, which previously caused a
    # hard HTTP 429 failure when this was split across multiple fetches.
    raw_calendar = fetch_raw_calendar_events()
    mql5_actuals = fetch_mql5_calendar_actuals()
    raw_calendar = apply_mql5_actuals(raw_calendar, mql5_actuals)
    calendar_events, calendar_is_fallback, calendar_date_label = select_todays_or_recent_from(raw_calendar)
    calendar_block = format_calendar_context(calendar_events, calendar_is_fallback, calendar_date_label)
    targeted_headlines_block = fetch_targeted_headlines_for_missing_actuals(calendar_events)

    yesterday_state = load_last_analysis()
    if yesterday_state:
        yesterday_block = f"({yesterday_state['date']}):\n{yesterday_state['analysis']}"
    else:
        yesterday_block = "No prior day's analysis available (first run, or state file missing)."

    oil_data = fetch_oil_price_data()
    oil_block = format_oil_context(oil_data)
    nets_consistency_block = build_nets_consistency_block()
    invalidation_consistency_block = build_invalidation_consistency_block()

    prompt = (
        CARRY_TRADE_FRAMEWORK + "\n\n" + GEOPOLITICAL_OIL_FRAMEWORK + "\n\n" + ANALYSIS_STYLE_GUIDE +
        "\n\nYESTERDAY'S ANALYSIS (for continuity - see instructions in WHAT CHANGED above for "
        "how to use this):\n" +
        yesterday_block +
        "\n\nCURRENT PRICE DATA (use ONLY these numbers if you reference price at all):\n" +
        price_block +
        "\n\nPIN-BAR LEVEL SETUP CHECK (a specific, already-decided trading rule - see "
        "instructions in DIRECTIONAL VIEW above for how to use this):\n" +
        setup_note +
        "\n\nOIL PRICE DATA AND GEOPOLITICAL CONTEXT (you MUST walk through all five layers of "
        "the GEOPOLITICAL OIL TRANSMISSION CHAIN internally and produce only the two-sentence "
        "synthesis):\n" +
        oil_block +
        "\n\nECONOMIC CALENDAR DATA (see instructions in WHAT CHANGED above for how to use this "
        "- quote any Actual figure directly from here, do not approximate):\n" +
        calendar_block +
        (
            "\n\nTARGETED HEADLINE SEARCH FOR MISSING ACTUALS (the calendar feed above marked "
            "these events' actual figures as not reported - these are dedicated news searches for "
            "each one specifically. If a headline states the real figure, use it and note it came "
            "from news coverage, not the calendar feed. If none of these mention a figure either, "
            "say the outcome is not yet confirmed rather than inventing one):\n" + targeted_headlines_block
            if targeted_headlines_block else ""
        ) +
        "\n\nHere are today's raw headline pulls on Fed policy, BOJ/yen intervention, "
        "gold, rate-hike odds, and oil/geopolitical coverage. Some headlines may be repetitive or "
        "low-value - ignore those and focus on what is actually new or market-moving. Use the "
        "oil/geopolitical headlines specifically to classify the shock in LAYER 1 of the chain:" +
        news_block +
        nets_consistency_block +
        invalidation_consistency_block
    )

    analysis_raw = ask_llm(prompt)
    date_str = datetime.date.today().strftime("%b %d, %Y")
    page_timestamp = current_display_timestamp()
    warning_banner = None

    if is_gemini_error(analysis_raw):
        print(f"Gemini call failed this run: {analysis_raw}")
        cached = load_last_analysis()
        if cached and cached.get("analysis"):
            # Fall back to the last successful report, clearly labeled as cached,
            # instead of publishing/sending the raw error text as if it were real
            # analysis. Reuse that day's own price/setup/calendar context too, so
            # the whole report stays internally consistent as an explicit replay -
            # not today's fresh numbers paired with yesterday's stale narrative.
            analysis = cached["analysis"]
            sections = parse_sections(analysis)
            price_block = cached.get("price_block", price_block)
            setup_note = cached.get("setup_note", setup_note)
            calendar_block = cached.get("calendar_block", calendar_block)
            cached_label = cached.get("page_timestamp") or cached.get("date", "an earlier run")
            warning_banner = (
                f"AI analysis service was unavailable this run ({analysis_raw[:120]}). Showing the "
                f"last successful report instead, originally generated {cached_label}. Treat all "
                f"figures and dates below as reflecting that earlier time, not now."
            )
            # Do NOT call save_last_analysis here - the state file must keep pointing
            # at the true last-fresh day, or tomorrow's continuity messaging (and any
            # further fallback) would lose track of what was actually last generated.
        else:
            print("No cached report available to fall back to - skipping publish entirely this run.")
            return
    else:
        analysis, extracted_actuals = extract_and_strip_actuals(analysis_raw)
        analysis = strip_section_refs(analysis)
        sections = parse_sections(analysis)

        # Backfill any actual figures the model found in news headlines back into the
        # calendar data itself. Since calendar_events shares the same dict objects as
        # raw_calendar (select_todays_or_recent_from filters, doesn't copy), this
        # mutation is automatically visible in raw_calendar too - so the archive
        # below needs no separate re-application step.
        calendar_events = apply_extracted_actuals(calendar_events, extracted_actuals)
        calendar_block = format_calendar_context(calendar_events, calendar_is_fallback, calendar_date_label)

        save_last_analysis(date_str, analysis, extra={
            "page_timestamp": page_timestamp,
            "price_block": price_block,
            "setup_note": setup_note,
            "calendar_block": calendar_block,
        })

        # Cross-run consistency guardrails - only persisted on a genuine fresh
        # analysis, same rule as save_last_analysis above, so a cached/fallback
        # run never overwrites these with a stale replay of yesterday's values.
        nets = extract_net_implications(analysis)
        if len(nets) >= 3:
            save_last_nets(nets, date_str)
        else:
            print(f"Only found {len(nets)}/3 'Net implication' lines this run - skipping nets-consistency save.")
        if sections.get("mind"):
            save_last_invalidation(sections["mind"], date_str)

    data_box_html = ""
    if price_data or warning_banner:
        data_box_html = (
            '<div style="font-family:monospace;font-size:12px;color:#444;background:#f4f4f4;'
            'padding:12px 14px;border-radius:6px;white-space:pre-wrap;margin-bottom:8px;">'
            + escape_html(price_block) + "</div>"
            '<div style="font-family:monospace;font-size:12px;color:#444;background:#eef6ff;'
            'padding:12px 14px;border-radius:6px;white-space:pre-wrap;margin-bottom:8px;">'
            + escape_html(setup_note) + "</div>"
            '<div style="font-family:monospace;font-size:12px;color:#444;background:#fff8e1;'
            'padding:12px 14px;border-radius:6px;white-space:pre-wrap;margin-bottom:14px;">'
            + escape_html(calendar_block) + "</div>"
        )

    html_out = build_newsletter_html(
        "Daily Gold / Macro Brief", f"Generated {page_timestamp}", sections, analysis, data_box_html,
        nav_links=[("Weekly COT →", "weekly.html"), ("Archive", "archive/"), ("Home", "index.html")],
        warning_banner=warning_banner,
    )
    # Archive copies live one folder deeper (docs/archive/), so their nav links need
    # a "../" prefix to point back to the top-level pages correctly.
    html_out_archived = build_newsletter_html(
        "Daily Gold / Macro Brief", f"Generated {page_timestamp}", sections, analysis, data_box_html,
        nav_links=[("Weekly COT →", "../weekly.html"), ("Archive", "."), ("Home", "../index.html")],
        warning_banner=warning_banner,
    )

    os.makedirs("docs/archive", exist_ok=True)
    with open("docs/daily.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    with open(f"docs/archive/daily-{datetime.date.today().isoformat()}.html", "w", encoding="utf-8") as f:
        f.write(html_out_archived)

    # Archive the FULL week's already-fetched data rather than a narrow days_back
    # window - the raw feed is naturally bounded to "this week" already, and a
    # fixed reachback (e.g. 2 days) can miss real events depending on which day
    # of the week the script happens to run (confirmed: 11 real events existed
    # this week but 0 fell within a 2-day-back window from a Saturday run).
    # raw_calendar already reflects both the MQL5 and news-headline backfills
    # applied above (shared dict objects), so no further merging is needed here.
    save_calendar_archive(date_str, datetime.date.today().isoformat(), raw_calendar)
    rebuild_archive_index()

    email_subject = "Daily Gold/Macro Brief - " + date_str
    telegram_subtitle = f"Generated {page_timestamp}"
    if warning_banner:
        email_subject = "[CACHED] " + email_subject
        telegram_subtitle = f"\u26A0\uFE0F CACHED - {telegram_subtitle}"

    maybe_send_email(email_subject, analysis, html_out)

    telegram_digest = build_telegram_digest("Daily Gold / Macro Brief", telegram_subtitle, sections, raw_analysis=analysis)
    maybe_send_telegram(telegram_digest)


if __name__ == "__main__":
    main()
