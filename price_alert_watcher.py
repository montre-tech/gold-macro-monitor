"""
Price Breakout Watcher.
Runs every 5 minutes (per the schedule in .github/workflows/price-alert-watcher.yml)
to catch a low-timeframe (5-minute) candle opening beyond the previous 30-minute
candle's range, while price sits inside yesterday's full pin-bar wick range (either
zone). Fires an instant Telegram alert on a NEW breakout - no Gemini call here, this
is a fast factual ping. The full macro interpretation still happens once a day in
daily_brief.py.

Needs yesterday's pin-zone data to already be cached (written by daily_brief.py each
time it runs) - if the daily brief hasn't run yet today, this check skips quietly
rather than making its own expensive daily-series API call.
"""
from lib import check_price_breakout_alert, maybe_send_telegram


def main():
    alert = check_price_breakout_alert()
    if alert:
        maybe_send_telegram(alert)
        print("Breakout alert sent.")
    else:
        print("No new breakout this check.")


if __name__ == "__main__":
    main()
