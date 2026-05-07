# TrendForce Daily Price Report

這個專案會每天抓取 TrendForce 台灣站「價格趨勢」公開頁面中的報價表，保存每日 JSON 快照，產生 Markdown 報告，並在每天早上 07:30 Asia/Taipei 自動寄送報告到 `w0617w0617@gmail.com`。

## What it collects

程式會抓取 TrendForce「價格趨勢」導覽下的公開分類頁面，目前涵蓋：

- DRAM
- NAND Flash
- TFT-LCD
- PV
- Li-Ion Battery

每個公開表格會記錄：

- 產品名稱
- TrendForce 頁面上顯示的所有報價欄位
- 資料來源頁面
- TrendForce `Last Update`
- 與 repository 中前一次保存資料的每日比較
- 如果同一產品名稱同時出現在 spot table 與 contract/future table，會計算 spot-vs-contract/future spread

報告內容包含產品名稱、報價欄位、與前一次保存資料的漲跌幅度，以及 Spot Price 與 Contract Price 可配對時的價差。TrendForce 頁面多數使用 `Contract Price`，程式在報告中會把它視為「期貨/合約」來和現貨計算價差。

## GitHub Actions schedule

GitHub Actions workflow 位於：

```text
.github/workflows/daily-trendforce-report.yml
```

排程設定如下：

```yaml
- cron: "30 23 * * *"
```

GitHub Actions 的 cron 使用 UTC，因此：

```text
23:30 UTC = 台灣時間隔天 07:30 Asia/Taipei
```

workflow 也支援手動執行：

```yaml
workflow_dispatch:
```

所以可以在 GitHub repository 的 `Actions` 頁面手動執行。

## Required GitHub Secrets

啟用 email delivery 前，請到 GitHub repository：

```text
Settings -> Secrets and variables -> Actions -> New repository secret
```

新增以下 secrets：

| Secret | Description |
| --- | --- |
| `SMTP_HOST` | SMTP server hostname，例如 Gmail 使用 `smtp.gmail.com`。 |
| `SMTP_PORT` | SMTP port，通常 `587` 用於 STARTTLS，`465` 用於 SSL。 |
| `SMTP_USERNAME` | SMTP 登入帳號。 |
| `SMTP_PASSWORD` | SMTP 密碼或 Gmail App Password。 |
| `SMTP_FROM` | Optional sender address。若未設定，預設使用 `SMTP_USERNAME`。 |
| `SMTP_USE_SSL` | Optional。只有使用 implicit SSL，例如 port `465` 時才設定為 `true`。 |

如果使用 Gmail，請先開啟兩步驟驗證，然後建立 App Password。不能直接使用一般 Gmail 登入密碼。

Gmail 常見設定：

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-account@gmail.com
SMTP_PASSWORD=your-app-password
```

## Run locally

建立 Python virtual environment 並安裝套件：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

不寄信，只測試產生資料與報告：

```bash
python src/trendforce_report.py --skip-email-if-missing-secrets
```

若要在本機寄信，請先設定 SMTP 環境變數：

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USERNAME=your-account@gmail.com
export SMTP_PASSWORD=your-app-password
export SMTP_FROM=your-account@gmail.com
```

然後執行：

```bash
python src/trendforce_report.py --send-email
```

## Generated files

產生的檔案會寫入：

```text
data/history/YYYY-MM-DD.json
reports/trendforce_daily_report_YYYY-MM-DD.md
```

第一次執行時，因為沒有前一次保存的資料，所以每日比較可能會顯示 `N/A`。從第二次執行開始，程式會用前一次保存的資料計算漲跌與差異。

## Notes

TrendForce 頁面結構若改版，parser 可能需要跟著調整。

這個 workflow 每天只抓取 5 個分類頁一次，避免不必要的高頻請求。