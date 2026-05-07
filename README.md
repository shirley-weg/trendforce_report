# TrendForce Daily Price Report

這個專案每天抓取 TrendForce 台灣站「價格趨勢」公開頁面中的報價表，產生每日 PDF 報告並寄到 `w0617w0617@gmail.com`。

目前涵蓋分類：

- DRAM
- NAND Flash
- TFT-LCD
- PV
- Li-Ion Battery

報告內容包含產品名稱、報價欄位、與前一次保存資料的漲跌幅度，以及 Spot Price 與 Contract Price 可配對時的價差。TrendForce 頁面多數使用 `Contract Price`，程式在報告中把它視為「期貨/合約」來和現貨計算價差。

## GitHub Actions 設定

workflow 已設定為台灣時間每天早上 7:30 執行，也可以手動執行 `workflow_dispatch`。

請在 GitHub repository 的 `Settings -> Secrets and variables -> Actions` 新增以下 secrets：

- `SMTP_HOST`：例如 Gmail 使用 `smtp.gmail.com`
- `SMTP_PORT`：例如 Gmail 使用 `587`
- `SMTP_USERNAME`：寄件 Gmail 帳號
- `SMTP_PASSWORD`：SMTP 密碼或 Gmail App Password
- `MAIL_FROM`：寄件人信箱，通常同 `SMTP_USERNAME`

如果使用 Gmail，帳號需要開啟兩步驟驗證並建立 App Password，不能直接使用一般登入密碼。

## 本機執行

```bash
pip install -r requirements.txt
python src/trendforce_report.py --save-history
```

若要寄信：

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USERNAME=your-account@gmail.com
export SMTP_PASSWORD=your-app-password
export MAIL_FROM=your-account@gmail.com
python src/trendforce_report.py --save-history --send-email --mail-to w0617w0617@gmail.com
```

## 歷史資料

`data/trendforce_history.json` 會保存最新一次抓到的資料。下一次執行時，程式會用這份資料計算「昨日比較」。第一次執行時沒有前一份資料，所以昨日比較會顯示 `N/A`。

GitHub Actions 會在每次成功寄出報告後，把新的歷史資料與 `reports/latest.html`、`reports/latest.md`、`reports/latest.pdf` commit 回 repository。Gmail 內容只會是一段簡短通知，完整報告會放在 PDF 附件中。

## 注意事項

TrendForce 頁面結構若改版，parser 可能需要跟著調整。這個 workflow 每天只抓取 5 個分類頁一次，避免不必要的高頻請求。
