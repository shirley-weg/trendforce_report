#!/usr/bin/env python3
"""Scrape TrendForce public price pages and email a daily report."""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_URL = "https://www.trendforce.com.tw"
PRICE_CATEGORY_URLS = [
    "https://www.trendforce.com.tw/price/dram/dram_spot",
    "https://www.trendforce.com.tw/price/flash/flash_spot",
    "https://www.trendforce.com.tw/price/lcd/panel",
    "https://www.trendforce.com.tw/price/pv/polysilicon",
    "https://www.trendforce.com.tw/price/battery-price/battery_cell_and_pack",
]
DEFAULT_RECIPIENT = "w0617w0617@gmail.com"
USER_AGENT = (
    "Mozilla/5.0 (compatible; TrendForceDailyReporter/1.0; "
    "+https://github.com/)"
)
AVERAGE_COLUMNS = ("盤平均", "均價", "平均", "Avg", "Average")
HIGH_COLUMNS = ("盤高點", "高點", "日高點", "週高點", "High")
LOW_COLUMNS = ("盤低點", "低點", "日低點", "週低點", "Low")
CHANGE_COLUMNS = ("盤漲跌幅", "均價漲跌", "漲跌幅", "Change")


@dataclass(slots=True)
class PriceQuote:
    category: str
    table: str
    source_url: str
    last_update: str | None
    product_name: str
    quote: dict[str, str]
    quote_value: float | None
    source_change_percent: float | None
    yesterday_change_percent: float | None = None
    spread_value: float | None = None
    spread_label: str | None = None

    @property
    def stable_key(self) -> str:
        return "|".join([self.category, self.table, self.product_name])


def fetch_html(url: str, timeout: int = 45) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return fragment


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(strip_tags(value))).strip()


def number_from_text(value: str | None) -> float | None:
    if not value:
        return None
    text = value.replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def category_title(html: str, fallback_url: str) -> str:
    for match in re.finditer(r"<(h1|h2)\b[^>]*>(.*?)</\1>", html, flags=re.I | re.S):
        text = clean_text(match.group(2))
        if "價格趨勢" in text:
            return text.replace("價格趨勢", "").strip() or text
    return fallback_url.rstrip("/").split("/")[-2]


def extract_table_rows(table_html: str) -> tuple[list[str], list[list[str]]]:
    rows: list[list[str]] = []
    for row_match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S):
        row_html = row_match.group(1)
        cells = [clean_text(cell.group(2)) for cell in re.finditer(r"<(th|td)\b[^>]*>(.*?)</\1>", row_html, flags=re.I | re.S)]
        cells = [cell for cell in cells if cell and cell != "走勢圖"]
        if cells:
            rows.append(cells)
    if not rows:
        return [], []
    headers = rows[0]
    return headers, rows[1:]


def choose_column(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for candidate in candidates:
        for index, header in enumerate(headers):
            if candidate.lower() in header.lower():
                return index
    return None


def row_to_quote(headers: list[str], cells: list[str]) -> dict[str, str]:
    quote: dict[str, str] = {}
    for index, header in enumerate(headers):
        if index < len(cells):
            quote[header] = cells[index]
    return quote


def quote_value(headers: list[str], quote: dict[str, str]) -> float | None:
    for candidates in (AVERAGE_COLUMNS, HIGH_COLUMNS, LOW_COLUMNS):
        idx = choose_column(headers, candidates)
        if idx is not None and idx < len(headers):
            parsed = number_from_text(quote.get(headers[idx]))
            if parsed is not None:
                return parsed
    return None


def title_before_table(prefix_html: str) -> str:
    headings = list(re.finditer(r"<(h2|h3|h4|a)\b[^>]*>(.*?)</\1>", prefix_html, flags=re.I | re.S))
    for match in reversed(headings[-12:]):
        text = clean_text(match.group(2))
        if text and ("Price" in text or "未稅" in text or text in {"多晶矽", "矽晶圓", "電池片", "模組", "光伏玻璃"}):
            return text
    text_prefix = clean_text(prefix_html[-1200:])
    match = re.search(r"([^。\n|]*?(?:Price|多晶矽|矽晶圓|電池片|模組|光伏玻璃)[^。\n|]*?)(?:Last Update|項目)", text_prefix)
    return match.group(1).strip() if match else "未命名報價表"


def last_update_before_table(prefix_html: str) -> str | None:
    text_prefix = clean_text(prefix_html[-1600:])
    matches = list(re.finditer(r"Last Update\s+(.+?)(?=\s+項目\s|$)", text_prefix))
    if not matches:
        return None
    return matches[-1].group(1).strip()


def parse_price_page(html: str, url: str) -> list[PriceQuote]:
    category = category_title(html, url)
    quotes: list[PriceQuote] = []
    for table_match in re.finditer(r"<table\b[^>]*>.*?</table>", html, flags=re.I | re.S):
        table_html = table_match.group(0)
        headers, rows = extract_table_rows(table_html)
        if not headers or ("項目" not in "".join(headers) and "品牌" not in "".join(headers)):
            continue
        name_idx = 0
        change_idx = choose_column(headers, CHANGE_COLUMNS)
        prefix = html[: table_match.start()]
        title = title_before_table(prefix)
        updated_at = last_update_before_table(prefix)
        for cells in rows:
            if len(cells) <= name_idx:
                continue
            quote = row_to_quote(headers, cells)
            name_parts = [cells[0]]
            if headers[0] == "品牌" and len(cells) >= 4:
                name_parts = cells[:4]
            product_name = clean_text(" ".join(name_parts))
            if not product_name:
                continue
            value = quote_value(headers, quote)
            source_change = None
            if change_idx is not None and change_idx < len(cells):
                source_change = number_from_text(cells[change_idx])
            quotes.append(PriceQuote(category, title, url, updated_at, product_name, quote, value, source_change))
    return quotes


def scrape_all(urls: Iterable[str]) -> list[PriceQuote]:
    all_quotes: list[PriceQuote] = []
    failures: list[str] = []
    for url in urls:
        try:
            all_quotes.extend(parse_price_page(fetch_html(url), url))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            failures.append(f"{url}: {exc}")
    if failures:
        raise RuntimeError("TrendForce fetch failed:\n" + "\n".join(failures))
    return all_quotes


def load_history(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return {item["stable_key"]: item for item in data.get("quotes", [])}


def add_comparisons(quotes: list[PriceQuote], yesterday: dict[str, dict]) -> None:
    for quote in quotes:
        previous = yesterday.get(quote.stable_key)
        previous_value = previous.get("quote_value") if previous else None
        if quote.quote_value is not None and previous_value not in (None, 0):
            quote.yesterday_change_percent = ((quote.quote_value - float(previous_value)) / float(previous_value)) * 100


def add_spreads(quotes: list[PriceQuote]) -> None:
    spot_lookup: dict[tuple[str, str], PriceQuote] = {}
    for quote in quotes:
        lower = f"{quote.table} {quote.product_name}".lower()
        if "spot" in lower or "現貨" in lower:
            spot_lookup[(quote.category, quote.product_name)] = quote
    for quote in quotes:
        lower = f"{quote.table} {quote.product_name}".lower()
        is_future_or_contract = any(token in lower for token in ("contract", "future", "期貨", "合約"))
        spot = spot_lookup.get((quote.category, quote.product_name))
        if is_future_or_contract and spot and quote.quote_value is not None and spot.quote_value is not None:
            quote.spread_value = quote.quote_value - spot.quote_value
            quote.spread_label = "正價差" if quote.spread_value > 0 else "負價差" if quote.spread_value < 0 else "零價差"


def serialise_snapshot(quotes: list[PriceQuote], generated_at: str) -> dict:
    return {
        "generated_at": generated_at,
        "source": BASE_URL,
        "quotes": [{"stable_key": quote.stable_key, **asdict(quote)} for quote in quotes],
    }


def format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def format_spread(quote: PriceQuote) -> str:
    if quote.spread_value is None:
        return "N/A"
    return f"{quote.spread_value:+.4f} ({quote.spread_label})"


def generate_markdown_report(quotes: list[PriceQuote], generated_at: str) -> str:
    lines = [
        f"# TrendForce 每日價格趨勢報告",
        "",
        f"產生時間：{generated_at}",
        f"資料來源：{BASE_URL}/ 價格趨勢公開頁面",
        "",
        "> 昨日比較以本 repo 前一日快照的主要報價欄位（優先使用均價/盤平均）計算。若前一日無相同品項資料則顯示 N/A。",
        "",
        "| 類別 | 報價表 | 產品名稱 | Last Update | 報價資料 | 與昨天比較 | 現貨/期貨或合約價差 |",
        "| --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for quote in sorted(quotes, key=lambda item: (item.category, item.table, item.product_name)):
        quote_data = "; ".join(f"{key}: {value}" for key, value in quote.quote.items())
        lines.append(
            "| "
            + " | ".join(
                [
                    quote.category,
                    quote.table,
                    quote.product_name,
                    quote.last_update or "N/A",
                    quote_data.replace("|", "/"),
                    format_percent(quote.yesterday_change_percent),
                    format_spread(quote),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def send_email(subject: str, markdown_body: str, recipient: str, attachment_path: Path | None = None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or "587")
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("SMTP_FROM") or username
    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(markdown_body)
    if attachment_path and attachment_path.exists():
        message.add_attachment(
            attachment_path.read_bytes(),
            maintype="text",
            subtype="markdown",
            filename=attachment_path.name,
        )

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--history-dir", default="data/history")
    parser.add_argument("--recipient", default=os.environ.get("REPORT_RECIPIENT", DEFAULT_RECIPIENT))
    parser.add_argument("--send-email", action="store_true")
    parser.add_argument("--skip-email-if-missing-secrets", action="store_true")
    args = parser.parse_args(argv)

    tz = ZoneInfo("Asia/Taipei")
    today = datetime.now(tz).date()
    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    output_dir = Path(args.output_dir)
    history_dir = Path(args.history_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)

    quotes = scrape_all(PRICE_CATEGORY_URLS)
    previous_snapshot = history_dir / f"{today - timedelta(days=1)}.json"
    add_comparisons(quotes, load_history(previous_snapshot))
    add_spreads(quotes)

    snapshot_path = history_dir / f"{today}.json"
    snapshot_path.write_text(json.dumps(serialise_snapshot(quotes, generated_at), ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = output_dir / f"trendforce_daily_report_{today}.md"
    markdown = generate_markdown_report(quotes, generated_at)
    report_path.write_text(markdown, encoding="utf-8")

    if args.send_email:
        required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"]
        missing = [name for name in required if not os.environ.get(name)]
        if missing and args.skip_email_if_missing_secrets:
            print(f"Skip email because missing secrets: {', '.join(missing)}", file=sys.stderr)
        elif missing:
            raise RuntimeError(f"Missing SMTP environment variables: {', '.join(missing)}")
        else:
            send_email(f"TrendForce 每日價格趨勢報告 {today}", markdown, args.recipient, report_path)

    print(f"Wrote {len(quotes)} quotes to {report_path} and {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
