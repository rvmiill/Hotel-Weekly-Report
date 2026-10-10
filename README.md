# Weekly Hotel Performance Report (automated)

Every Monday at 08:00 (Malaysia time) this pulls last week's numbers from a Google Sheet, rates each hotel against its peers, has Claude write a personalised message per hotel, and emails it. In test mode every email goes to your own inbox only.

```
Google Sheet -> Python (benchmark + classify) -> Claude (write message) -> Gmail SMTP
                                                   ^ verified facts only, output validated
GitHub Actions cron fires it every Monday
```

Expected sheet columns (one row per hotel): `brandname, revenue, Order, Boss Gmail`.
Column names can be changed in `.env` (`COL_HOTEL`, `COL_REVENUE`, `COL_ORDERS`, `COL_EMAIL`).

## Requirements
- Python 3.10+
- An Anthropic API key
- A Gmail account with 2-Step Verification (for an App Password)

## Setup

1. **Share the sheet**: Share -> "Anyone with the link" -> Viewer. Copy the sheet's address (or just the ID from `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`). It must be a real Google Sheet, not an uploaded file preview.
2. **Get keys**
   - Anthropic API key: console.anthropic.com -> API keys.
   - Gmail App Password: Google Account -> Security -> turn on 2-Step Verification -> App passwords. Use this, not your normal password.
3. **Configure**: copy `.env.example` to `.env` and fill in `SHEET_ID`, `ANTHROPIC_API_KEY`, `SMTP_USER`, `SMTP_APP_PASSWORD`, `TEST_RECIPIENT`. Optionally set `CURRENCY` (for example `RM`); it is blank by default. Keep values free of quotes and trailing comments.
4. **Install**: `pip install -r requirements.txt`
5. **Safe dry run** (no email, files are saved to `outbox/`, including `summary.csv` with every hotel's status):
   - Windows PowerShell: `$env:DRY_RUN="true"; py weekly_report.py`
   - Mac/Linux: `DRY_RUN=true python3 weekly_report.py`
   - To test without Google Sheets, use any CSV with the expected columns: set `LOCAL_CSV=path/to/file.csv`.
6. **Real test**: `py weekly_report.py` (or `python3 ...`). Every email goes to `TEST_RECIPIENT`, with the intended owner shown in the subject.
7. **Automate**: push to GitHub, then add repository secrets under Settings -> Secrets and variables -> Actions: `SHEET_ID`, `ANTHROPIC_API_KEY`, `SMTP_USER`, `SMTP_APP_PASSWORD`, `TEST_RECIPIENT`. Run it once from Actions -> "Weekly hotel report" -> Run workflow. After that it runs every Monday (cron `0 0 * * 1` = 08:00 Malaysia time; GitHub may start it a few minutes late).
8. **Go live** (only when you really mean it): set `SEND_TO_REAL_OWNERS=true` so each owner receives their own email.

## How it decides
- The sheet has no targets or history, so each hotel is compared with the **typical (median) hotel that had at least one booking**: a revenue index and a bookings index.
- **STRONG**: at or above 100% on both. **ON_TRACK**: 75-99%. **NEEDS_IMPROVEMENT**: below 75% on either, or zero bookings.
- A focus is derived from the numbers: low bookings with healthy spend per booking -> "booking volume"; the reverse -> "value per booking"; zero bookings -> asks about the cause instead of guessing.
- Claude only writes the wording from those verified facts. The reply is checked to contain the exact bookings and revenue figures; otherwise a template message is sent, so reports still go out if the API is down.
- If the sheet gets `revenue_target` / `order_target` columns, they are used automatically instead of the peer benchmark.
- Thresholds can be tuned with `STRONG_AT`, `NEEDS_BELOW` and `AOV_OK` in `.env`.

## Troubleshooting
- **Emails are only saved to `outbox/`**: `DRY_RUN` is `true`, or a required email setting is missing. The script prints `[not sent] ...` with the reason.
- **Settings typed in the terminal override `.env`** and last until the terminal closes. Clear them with `Remove-Item Env:DRY_RUN, Env:LOCAL_CSV, Env:SHEET_ID -ErrorAction SilentlyContinue` (PowerShell).
- **`HTTP 400` / `403` from Google**: check the sheet is shared as "Anyone with the link" and is a real Google Sheet.
- **Gmail login error**: create a new App Password and paste it without spaces.
- **Never commit `.env`**. It is listed in `.gitignore`.

See `WRITEUP.md` for why these tools and thresholds were chosen and how the AI messages are designed.