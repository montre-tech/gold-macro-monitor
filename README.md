# Gold & Macro Intelligence — GitHub-hosted version

Same idea as the Google Apps Script version, but hosted entirely on GitHub:
GitHub Actions runs the scripts on a schedule, publishes a styled newsletter page
via GitHub Pages, and (optionally) emails it to you too. Cost: **$0** — GitHub
Actions is free for public repos, and Gemini's free tier needs no credit card.

- `daily_brief.py` — pulls Fed/BOJ/gold news and asks Gemini for a structured analysis.
- `weekly_cot.py` — pulls CFTC's Commitments of Traders gold data, computes trend/percentile
  context and long-vs-short asymmetry in code, then asks Gemini to write the analysis.
- `lib.py` — shared helpers (Gemini calls, HTML rendering, optional email).
- `docs/` — the published newsletter, served by GitHub Pages.

## Setup (about 10 minutes)

1. **Create the repo.** Create a new GitHub repository and push these files to it
   (or upload them directly through the GitHub web UI: Add file → Upload files).

2. **Get a free Gemini API key.** Go to https://aistudio.google.com/app/apikey,
   sign in, and create a key — no billing required for the free tier.

3. **Add repository secrets.** In your repo: Settings → Secrets and variables →
   Actions → New repository secret. Add:
   - `GEMINI_API_KEY` (required) — the key from step 2.
   - `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_TO` (optional) — only if you also want it
     emailed to you. Use a Gmail address for `EMAIL_USER` and a Gmail
     [App Password](https://myaccount.google.com/apppasswords) (not your normal
     password) for `EMAIL_PASS`. If you skip these, the script just publishes to
     GitHub Pages and skips email — nothing breaks.

4. **Enable GitHub Pages.** Settings → Pages → Source: "Deploy from a branch" →
   Branch: `main`, folder: `/docs` → Save. GitHub will give you a URL like
   `https://yourusername.github.io/your-repo-name/`.

5. **Test it manually.** Go to the "Actions" tab → click "Daily Gold/Macro Brief"
   (or "Weekly Gold COT Analysis") → "Run workflow" → Run workflow. Wait a minute,
   then check the Actions log for errors, and visit your Pages URL to see the result.

6. **Done.** The schedules in the two workflow files (`.github/workflows/*.yml`)
   will now run automatically — daily at 12:00 UTC, and weekly on Saturdays at
   13:00 UTC (after Friday's CFTC release). Edit the `cron:` lines if you want
   different times; cron always uses UTC regardless of your local timezone.

## Where things show up

- `https://yourusername.github.io/your-repo-name/daily.html` — latest daily brief
- `https://yourusername.github.io/your-repo-name/weekly.html` — latest weekly COT analysis
- `https://yourusername.github.io/your-repo-name/archive/` — every past report, dated

## Troubleshooting

- **Workflow fails on the Gemini call** — check the Actions log for the exact error.
  If it says a model is no longer available, add a repo secret `GEMINI_MODEL` set to
  whatever name Google's error message recommends (the code already reads this from
  an env var, no code changes needed).
- **Workflow fails on `git push`** — make sure the workflow has `permissions: contents: write`
  (already set in both YAML files) and that your repo's Settings → Actions → General →
  Workflow permissions is set to "Read and write permissions".
- **CFTC data errors** — CFTC occasionally adjusts its API schema. Check the current
  field list at https://dev.socrata.com/foundry/publicreporting.cftc.gov/6dca-aqww.
