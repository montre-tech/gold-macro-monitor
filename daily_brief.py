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

    prompt = (
        CARRY_TRADE_FRAMEWORK + "\n\n" + ANALYSIS_STYLE_GUIDE +
        "\n\nHere are today's raw headline pulls on Fed policy, BOJ/yen intervention, "
        "gold, and rate-hike odds coverage. Some headlines may be repetitive or low-value - "
        "ignore those and focus on what is actually new or market-moving:" + news_block
    )

    analysis = ask_gemini(prompt)
    date_str = datetime.date.today().strftime("%b %d, %Y")
    sections = parse_sections(analysis)
    html_out = build_newsletter_html("Daily Gold / Macro Brief", date_str, sections, analysis)

    os.makedirs("docs/archive", exist_ok=True)
    with open("docs/daily.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    with open(f"docs/archive/daily-{datetime.date.today().isoformat()}.html", "w", encoding="utf-8") as f:
        f.write(html_out)

    maybe_send_email("Daily Gold/Macro Brief - " + date_str, analysis, html_out)


if __name__ == "__main__":
    main()
