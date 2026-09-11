"""
Daily Gold/Macro Brief.
Pulls news headlines on Fed policy, BOJ/yen intervention, and gold, then asks Gemini
to synthesize a structured analysis. Publishes to docs/daily.html (served by GitHub
Pages) and optionally emails it if EMAIL_USER/EMAIL_PASS secrets are set.
"""
import datetime
import os
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from lib import (
    CARRY_TRADE_FRAMEWORK,
    ANALYSIS_STYLE_GUIDE,
    ask_gemini,
    parse_sections,
    build_newsletter_html,
    maybe_send_email,
    fetch_gold_price_data,
    format_price_context,
    fetch_recent_30m_close,
    build_pin_bar_setup_note,
    fetch_economic_calendar,
    format_calendar_context,
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
]


def fetch_news(query, max_items=6):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en"
    resp = requests.get(url, timeout=20)
    root = ET.fromstring(resp.content)
    items = root.findall("./channel/item")[:max_items]
    lines = [f"- {item.findtext('title')} ({item.findtext('pubDate')})" for item in items]
    return "\n".join(lines)


def main():
    news_block = ""
    for q in NEWS_QUERIES:
        news_block += f'\n\nSearch: "{q}"\n{fetch_news(q)}'

    price_data = fetch_gold_price_data()

    if price_data:
        price_block = format_price_context(price_data)
        recent_30m = fetch_recent_30m_close()
        setup_note = build_pin_bar_setup_note(price_data, recent_30m)
    else:
        price_block = "Price data unavailable this run - do not state any specific price or price range."
        setup_note = "Setup check unavailable - price data was missing this run."

    calendar_events = fetch_economic_calendar()
    calendar_block = format_calendar_context(calendar_events)

    yesterday_state = load_last_analysis()
    if yesterday_state:
        yesterday_block = f"({yesterday_state['date']}):\n{yesterday_state['analysis']}"
    else:
        yesterday_block = "No prior day's analysis available (first run, or state file missing)."

    prompt = (
        CARRY_TRADE_FRAMEWORK + "\n\n" + ANALYSIS_STYLE_GUIDE +
        "\n\nYESTERDAY'S ANALYSIS (for continuity - see instructions in WHAT CHANGED above for "
        "how to use this):\n" +
        yesterday_block +
        "\n\nCURRENT PRICE DATA (use ONLY these numbers if you reference price at all):\n" +
        price_block +
        "\n\nPIN-BAR LEVEL SETUP CHECK (a specific, already-decided trading rule - see "
        "instructions in DIRECTIONAL VIEW above for how to use this):\n" +
        setup_note +
        "\n\nECONOMIC CALENDAR DATA (see instructions in ECONOMIC CALENDAR & POSITIONING "
        "above for how to use this):\n" +
        calendar_block +
        "\n\nHere are today's raw headline pulls on Fed policy, BOJ/yen intervention, "
        "gold, and rate-hike odds coverage. Some headlines may be repetitive or low-value - "
        "ignore those and focus on what is actually new or market-moving:" + news_block
    )

    analysis = ask_gemini(prompt)
    date_str = datetime.date.today().strftime("%b %d, %Y")
    page_timestamp = current_display_timestamp()
    sections = parse_sections(analysis)
    save_last_analysis(date_str, analysis)

    data_box_html = ""
    if price_data:
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
    )
    # Archive copies live one folder deeper (docs/archive/), so their nav links need
    # a "../" prefix to point back to the top-level pages correctly.
    html_out_archived = build_newsletter_html(
        "Daily Gold / Macro Brief", f"Generated {page_timestamp}", sections, analysis, data_box_html,
        nav_links=[("Weekly COT →", "../weekly.html"), ("Archive", "."), ("Home", "../index.html")],
    )

    os.makedirs("docs/archive", exist_ok=True)
    with open("docs/daily.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    with open(f"docs/archive/daily-{datetime.date.today().isoformat()}.html", "w", encoding="utf-8") as f:
        f.write(html_out_archived)

    save_calendar_archive(date_str, datetime.date.today().isoformat(), fetch_economic_calendar(days_back=2))
    rebuild_archive_index()

    maybe_send_email("Daily Gold/Macro Brief - " + date_str, analysis, html_out)


if __name__ == "__main__":
    main()
