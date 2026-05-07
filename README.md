# TrendForce Daily Price Report

This repository contains a GitHub Actions automation that scrapes the public **價格趨勢** pages on <https://www.trendforce.com.tw/>, stores a daily JSON snapshot, generates a Markdown report, and emails the report to `w0617w0617@gmail.com` every morning at 07:30 Asia/Taipei time.

## What it collects

The scraper visits the public category pages exposed under TrendForce's **價格趨勢** navigation:

- DRAM
- NAND Flash
- TFT-LCD
- PV
- Li-Ion Battery

For each public table it records:

- product name
- all quote columns shown by TrendForce
- source page and TrendForce `Last Update`
- daily comparison against this repository's previous-day snapshot
- spot-vs-contract/future spread when the same product name appears in both a spot table and a contract/future table

## GitHub Actions schedule

The workflow is in `.github/workflows/daily-trendforce-report.yml` and runs on this cron expression:

```yaml
- cron: "30 23 * * *"
```

GitHub Actions cron uses UTC, so `23:30 UTC` is `07:30 Asia/Taipei` the next morning.

## Required GitHub Secrets

Configure these repository secrets before enabling email delivery:

| Secret | Description |
| --- | --- |
| `SMTP_HOST` | SMTP server hostname, for example `smtp.gmail.com`. |
| `SMTP_PORT` | SMTP port, usually `587` for STARTTLS or `465` for SSL. |
| `SMTP_USERNAME` | SMTP login user. |
| `SMTP_PASSWORD` | SMTP password or app password. |
| `SMTP_FROM` | Optional sender address. Defaults to `SMTP_USERNAME`. |
| `SMTP_USE_SSL` | Optional. Set to `true` only when using implicit SSL such as port `465`. |

For Gmail, create an app password and use `smtp.gmail.com`, port `587`, and the Gmail address as `SMTP_USERNAME`.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python src/trendforce_report.py --skip-email-if-missing-secrets
```

To send email locally, export the SMTP variables and run:

```bash
python src/trendforce_report.py --send-email
```

Generated files are written to:

- `data/history/YYYY-MM-DD.json`
- `reports/trendforce_daily_report_YYYY-MM-DD.md`
