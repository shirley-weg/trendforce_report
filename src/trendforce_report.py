from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


TIMEZONE = ZoneInfo("Asia/Taipei")
BASE_URL = "https://www.trendforce.com.tw"
DEFAULT_MAIL_TO = "w0617w0617@gmail.com"

PRICE_PAGES = [
    {
        "category": "DRAM",
        "url": "https://www.trendforce.com.tw/price/dram/dram_spot",
    },
    {
        "category": "NAND Flash",
        "url": "https://www.trendforce.com.tw/price/flash/flash_spot",
    },
    {
        "category": "TFT-LCD",
        "url": "https://www.trendforce.com.tw/price/lcd/panel",
    },
    {
        "category": "PV",
        "url": "https://www.trendforce.com.tw/price/pv/polysilicon",
    },
    {
        "category": "Li-Ion Battery",
        "url": "https://www.trendforce.com.tw/price/battery-price/battery_cell_and_pack",
    },
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TrendForceDailyReport/1.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

PRICE_HEADER_HINTS = (
    "高點",
    "低點",
    "均價",
    "平均",
    "avg",
    "price",
    "last",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

PRIMARY_VALUE_HEADERS = (
    "盤平均",
    "均價",
    "平均",
    "avg",
)

CHANGE_HEADER_HINTS = ("漲跌", "change", "mom", "hoh", "%")


@dataclass
class FetchResult:
    category: str
    url: str
    html_text: str


def clean_text(value: Any) -> str:
    if isinstance(value, Tag):
        value = value.get_text(" ", strip=True)
    text = html.unescape(str(value or ""))
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_decimal(text: str | None) -> float | None:
    if not text:
        return None
    normalized = clean_text(text).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    try:
        return float(Decimal(match.group(0)))
    except (InvalidOperation, ValueError):
        return None


def unique_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for header in headers:
        base = header or "欄位"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result


def classify_section(title: str) -> str:
    lowered = title.lower()
    if "contract" in lowered or "期貨" in title or "合約" in title:
        return "contract"
    if "spot" in lowered or "street price" in lowered or "現貨" in title:
        return "spot"
    return "other"


def normalize_spread_key(product_name: str) -> str:
    text = clean_text(product_name).lower()
    text = re.sub(r"\((rmb|usd|ntd|twd)\)", " ", text, flags=re.I)
    text = re.sub(r"[()（）]", " ", text)
    text = re.sub(r"\b\d{3,5}(?:/\d{3,5})?\b", " ", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def record_key(record: dict[str, Any]) -> str:
    return "|".join(
        [
            record["category"],
            record["section"],
            record["product_name"],
            record.get("currency") or "",
        ]
    )


def detect_currency(product_name: str, row: dict[str, str], section_title: str) -> str | None:
    joined = " ".join([product_name, section_title, *row.values()])
    for currency in ("USD", "RMB", "TWD", "NTD", "$USD"):
        if currency.lower() in joined.lower():
            return "USD" if currency == "$USD" else currency
    return None


def find_first_price_column(headers: list[str]) -> int:
    for index, header in enumerate(headers):
        lowered = header.lower()
        if any(hint in header or hint in lowered for hint in PRICE_HEADER_HINTS):
            return index
    return 1 if len(headers) > 1 else 0


def find_primary_value(row: dict[str, str]) -> tuple[str | None, float | None]:
    for header, value in row.items():
        lowered = header.lower()
        if "last avg" in lowered:
            continue
        if any(hint in header or hint in lowered for hint in PRIMARY_VALUE_HEADERS):
            parsed = parse_decimal(value)
            if parsed is not None:
                return header, parsed

    for header, value in reversed(list(row.items())):
        lowered = header.lower()
        if any(hint in header or hint in lowered for hint in CHANGE_HEADER_HINTS):
            continue
        parsed = parse_decimal(value)
        if parsed is not None:
            return header, parsed
    return None, None


def find_change_percent(row: dict[str, str]) -> float | None:
    for header, value in row.items():
        lowered = header.lower()
        if any(hint in header or hint in lowered for hint in CHANGE_HEADER_HINTS):
            parsed = parse_decimal(value)
            if parsed is not None:
                return parsed
    return None


def get_section_update(section: Tag) -> str | None:
    update = section.select_one(".price-last-update")
    if not update:
        return None
    text = clean_text(update)
    return text.replace("Last Update", "").strip() or None


def parse_price_page(fetch: FetchResult, captured_at: datetime) -> list[dict[str, Any]]:
    soup = BeautifulSoup(fetch.html_text, "html.parser")
    records: list[dict[str, Any]] = []

    for section in soup.select(".price-content"):
        title_node = section.select_one(".price-title")
        section_title = clean_text(title_node)
        section_title = re.sub(r"\s*\(未稅\)\s*$", "", section_title).strip()
        if not section_title:
            continue

        table = section.select_one("table.price-table")
        if not table:
            continue

        headers = [
            clean_text(th)
            for th in table.select("thead th")
            if "走勢圖" not in clean_text(th)
        ]
        headers = unique_headers(headers)
        if not headers:
            continue

        first_price_column = find_first_price_column(headers)
        update_time = get_section_update(section)
        section_kind = classify_section(section_title)

        body_rows = table.select("tbody tr")
        rows = body_rows if body_rows else table.find_all("tr")
        for tr in rows:
            if tr.find_parent("thead") or tr.find_parent("tfoot"):
                continue
            cells = []
            for td in tr.find_all("td", recursive=False):
                if "desktop-only" in (td.get("class") or []) and "history-cell" in (td.get("class") or []):
                    continue
                if td.get("colspan"):
                    continue
                cells.append(clean_text(td))

            if len(cells) < 2:
                continue

            row_headers = headers[: len(cells)]
            if len(cells) > len(headers):
                row_headers += [f"extra_{i}" for i in range(len(headers) + 1, len(cells) + 1)]

            row = dict(zip(row_headers, cells))
            product_parts = [part for part in cells[:first_price_column] if part]
            product_name = " / ".join(product_parts) if product_parts else cells[0]
            primary_label, primary_value = find_primary_value(row)

            if primary_value is None and not any(parse_decimal(value) is not None for value in row.values()):
                continue

            record = {
                "captured_at": captured_at.isoformat(),
                "category": fetch.category,
                "section": section_title,
                "section_kind": section_kind,
                "source_url": fetch.url,
                "source_update": update_time,
                "product_name": product_name,
                "currency": detect_currency(product_name, row, section_title),
                "primary_value_label": primary_label,
                "primary_value": primary_value,
                "site_change_percent": find_change_percent(row),
                "quote": row,
                "spread_key": normalize_spread_key(product_name),
            }
            records.append(record)

    return records


def fetch_pages(timeout: int) -> list[FetchResult]:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    results: list[FetchResult] = []
    for page in PRICE_PAGES:
        response = session.get(page["url"], timeout=timeout)
        response.raise_for_status()
        results.append(
            FetchResult(
                category=page["category"],
                url=page["url"],
                html_text=response.text,
            )
        )
    return results


def load_previous_records(history_path: Path) -> dict[str, dict[str, Any]]:
    if not history_path.exists():
        return {}
    data = json.loads(history_path.read_text(encoding="utf-8"))
    records = data.get("records", [])
    return {record_key(record): record for record in records}


def add_daily_comparisons(
    current_records: list[dict[str, Any]],
    previous_records: dict[str, dict[str, Any]],
) -> None:
    for record in current_records:
        previous = previous_records.get(record_key(record))
        previous_value = previous.get("primary_value") if previous else None
        current_value = record.get("primary_value")
        record["previous_value"] = previous_value
        record["daily_change_percent"] = None
        if current_value is None or previous_value in (None, 0):
            continue
        record["daily_change_percent"] = ((current_value - previous_value) / previous_value) * 100


def add_spreads(records: list[dict[str, Any]]) -> None:
    spot_records: dict[tuple[str, str, str], dict[str, Any]] = {}
    contract_records: dict[tuple[str, str, str], dict[str, Any]] = {}

    for record in records:
        value = record.get("primary_value")
        if value is None:
            continue
        key = (
            record["category"],
            record.get("currency") or "",
            record.get("spread_key") or "",
        )
        if not key[2]:
            continue
        if record["section_kind"] == "spot":
            spot_records.setdefault(key, record)
        elif record["section_kind"] == "contract":
            contract_records.setdefault(key, record)

    for key, spot_record in spot_records.items():
        contract_record = contract_records.get(key)
        if not contract_record:
            continue
        spread = contract_record["primary_value"] - spot_record["primary_value"]
        spread_type = "正價差" if spread > 0 else "負價差" if spread < 0 else "零價差"
        payload = {
            "spot_section": spot_record["section"],
            "contract_section": contract_record["section"],
            "value": spread,
            "label": f"{spread:.4g} ({spread_type})",
        }
        spot_record["spot_contract_spread"] = payload
        contract_record["spot_contract_spread"] = payload


def quote_summary(record: dict[str, Any]) -> str:
    name_headers = set()
    headers = list(record["quote"].keys())
    first_price_column = find_first_price_column(headers)
    for header in headers[:first_price_column]:
        name_headers.add(header)

    parts = []
    for key, value in record["quote"].items():
        if key in name_headers or "走勢圖" in key:
            continue
        if value:
            parts.append(f"{key}: {value}")
    return "; ".join(parts)


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    direction = "▲" if value > 0 else "▼" if value < 0 else "—"
    return f"{direction} {value:.2f}%"


def fmt_number(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.4g}"


def build_markdown_report(records: list[dict[str, Any]], captured_at: datetime) -> str:
    lines = [
        f"# TrendForce 每日報價報告 - {captured_at:%Y-%m-%d}",
        "",
        f"抓取時間：{captured_at:%Y-%m-%d %H:%M:%S %Z}",
        "",
    ]
    for category in sorted({record["category"] for record in records}):
        lines.extend([f"## {category}", ""])
        category_records = [record for record in records if record["category"] == category]
        current_section = None
        for record in category_records:
            if record["section"] != current_section:
                current_section = record["section"]
                update = record.get("source_update") or "N/A"
                lines.extend([f"### {current_section}", f"Last Update: {update}", ""])
            spread = record.get("spot_contract_spread", {}).get("label", "")
            lines.append(
                "- "
                f"{record['product_name']} | "
                f"{quote_summary(record)} | "
                f"昨日比較: {fmt_percent(record.get('daily_change_percent'))}"
                + (f" | 現貨/期貨價差: {spread}" if spread else "")
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_html_report(records: list[dict[str, Any]], captured_at: datetime) -> str:
    rows = []
    for record in records:
        spread = record.get("spot_contract_spread", {}).get("label", "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(record['category'])}</td>"
            f"<td>{html.escape(record['section'])}</td>"
            f"<td>{html.escape(record['product_name'])}</td>"
            f"<td>{html.escape(quote_summary(record))}</td>"
            f"<td>{html.escape(fmt_number(record.get('previous_value')))}</td>"
            f"<td>{html.escape(fmt_percent(record.get('daily_change_percent')))}</td>"
            f"<td>{html.escape(spread or 'N/A')}</td>"
            f"<td>{html.escape(record.get('source_update') or 'N/A')}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>TrendForce 每日報價報告</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #d8dee4; padding: 8px; vertical-align: top; }}
    th {{ background: #f6f8fa; text-align: left; }}
    h1 {{ font-size: 22px; }}
    .meta {{ color: #57606a; }}
  </style>
</head>
<body>
  <h1>TrendForce 每日報價報告 - {captured_at:%Y-%m-%d}</h1>
  <p class="meta">抓取時間：{captured_at:%Y-%m-%d %H:%M:%S %Z}</p>
  <table>
    <thead>
      <tr>
        <th>分類</th>
        <th>產品群組</th>
        <th>產品名稱</th>
        <th>報價資料</th>
        <th>昨日主值</th>
        <th>昨日比較</th>
        <th>現貨/期貨價差</th>
        <th>來源更新時間</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>
"""


def save_history(history_path: Path, records: list[dict[str, Any]], captured_at: datetime) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": captured_at.isoformat(),
        "source": BASE_URL,
        "records": records,
    }
    history_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def send_email(subject: str, text_body: str, html_body: str, recipient: str) -> None:
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    mail_from = os.environ.get("MAIL_FROM") or smtp_user

    missing = [
        name
        for name, value in {
            "SMTP_HOST": smtp_host,
            "SMTP_USERNAME": smtp_user,
            "SMTP_PASSWORD": smtp_password,
            "MAIL_FROM/SMTP_USERNAME": mail_from,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email configuration: {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def run(args: argparse.Namespace) -> int:
    captured_at = datetime.now(TIMEZONE)
    history_path = Path(args.history)
    previous_records = load_previous_records(history_path)

    records: list[dict[str, Any]] = []
    for fetch in fetch_pages(timeout=args.timeout):
        records.extend(parse_price_page(fetch, captured_at))

    if not records:
        raise RuntimeError("No price records were parsed from TrendForce pages.")

    add_daily_comparisons(records, previous_records)
    add_spreads(records)

    html_report = build_html_report(records, captured_at)
    markdown_report = build_markdown_report(records, captured_at)

    write_report(Path(args.report_html), html_report)
    write_report(Path(args.report_md), markdown_report)

    if args.save_history:
        save_history(history_path, records, captured_at)

    if args.send_email:
        recipient = args.mail_to or os.environ.get("MAIL_TO") or DEFAULT_MAIL_TO
        send_email(
            subject=f"TrendForce 每日報價報告 {captured_at:%Y-%m-%d}",
            text_body=markdown_report,
            html_body=html_report,
            recipient=recipient,
        )

    print(f"Parsed {len(records)} records.")
    print(f"HTML report: {args.report_html}")
    print(f"Markdown report: {args.report_md}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily TrendForce price report.")
    parser.add_argument("--history", default="data/trendforce_history.json")
    parser.add_argument("--report-html", default="reports/latest.html")
    parser.add_argument("--report-md", default="reports/latest.md")
    parser.add_argument("--mail-to", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--save-history", action="store_true")
    parser.add_argument("--send-email", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
