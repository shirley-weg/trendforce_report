#!/usr/bin/env python3
"""Generate a daily TrendForce price report and email it with a PDF attachment."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from weasyprint import HTML


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
    "january",
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

TOOLTIP_ATTRIBUTES = (
    "title",
    "data-title",
    "data-original-title",
    "data-bs-title",
    "data-content",
    "data-bs-content",
    "aria-label",
)

NON_PRICE_SECTION_KEYWORDS = (
    "shipment",
    "出貨",
    "出货",
    "出貨量",
    "出货量",
)

NON_PRICE_HEADER_KEYWORDS = (
    "k units",
    "k unit",
    "k square",
    "worldwide",
    "area",
)


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


def normalize_company_text(value: Any, visible_text: str = "") -> str | None:
    text = clean_text(value)

    if not text:
        return None

    text = re.sub(r"<[^>]+>", " ", text)
    text = clean_text(text)

    if not text:
        return None

    if visible_text and text == clean_text(visible_text):
        return None

    lowered = text.lower()
    ignored_values = {
        "走勢圖",
        "history",
        "image",
        "more",
        "click here",
    }

    if lowered in ignored_values:
        return None

    if text.startswith("http://") or text.startswith("https://"):
        return None

    return text


def extract_company_names_from_cells(product_cells: list[Tag], product_name: str) -> str | None:
    company_names: list[str] = []
    seen: set[str] = set()

    for cell in product_cells:
        visible_text = clean_text(cell)
        nodes = [cell, *cell.find_all(True)]

        for node in nodes:
            for attr in TOOLTIP_ATTRIBUTES:
                raw_value = node.get(attr)

                if raw_value is None:
                    continue

                if isinstance(raw_value, list):
                    raw_value = " ".join(str(part) for part in raw_value)

                company_text = normalize_company_text(raw_value, visible_text or product_name)

                if not company_text or company_text in seen:
                    continue

                company_names.append(company_text)
                seen.add(company_text)

    if not company_names:
        return None

    return "、".join(company_names)


def product_name_for_report(record: dict[str, Any]) -> str:
    product_name = record["product_name"]
    company_names = record.get("company_names")

    if company_names:
        return f"{product_name} ({company_names})"

    return product_name


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

    if "contract" in lowered or "future" in lowered or "期貨" in title or "合約" in title:
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


def normalize_filter_text(value: Any) -> str:
    """Normalize text before deciding whether a table is a price quote table."""
    return clean_text(value).lower().replace("　", " ")


def is_non_price_section(section_title: str, headers: list[str]) -> bool:
    """Return True for tables that are market statistics, not price quotes.

    Example: TFT-LCD "Large Size Panel Shipment" contains shipment volume / area
    columns such as K units and K square. It is useful industry data, but it is
    not a quote table and should not appear in the daily price report.
    """
    lowered_title = normalize_filter_text(section_title)

    # Strong rule: any section whose title says shipment / 出貨 is not a price table.
    if any(keyword in lowered_title for keyword in NON_PRICE_SECTION_KEYWORDS):
        return True

    lowered_headers = [normalize_filter_text(header) for header in headers]
    matched_headers = sum(
        1
        for header in lowered_headers
        if any(keyword in header for keyword in NON_PRICE_HEADER_KEYWORDS)
    )

    return matched_headers >= 2


def is_non_price_record(record: dict[str, Any]) -> bool:
    """Final safety filter for rows that were parsed from non-price tables.

    This prevents shipment / volume statistics from appearing in the report even
    if the HTML structure changes and the section-level filter misses them.
    """
    category = normalize_filter_text(record.get("category", ""))
    section = normalize_filter_text(record.get("section", ""))
    product_name = normalize_filter_text(record.get("product_name", ""))
    quote = record.get("quote") or {}
    quote_keys = [normalize_filter_text(key) for key in quote.keys()]
    quote_values = [normalize_filter_text(value) for value in quote.values()]
    joined_quote = " ".join([*quote_keys, *quote_values])

    if any(keyword in section for keyword in NON_PRICE_SECTION_KEYWORDS):
        return True

    # Explicitly remove the TFT-LCD shipment table shown as
    # "Large Size Panel Shipment $USD". It is shipment volume / area, not price.
    if category == "tft-lcd" and "large size panel shipment" in section:
        return True

    # Shipment tables commonly contain K units / K square / Worldwide / Area.
    shipment_header_hits = sum(
        1 for keyword in NON_PRICE_HEADER_KEYWORDS if keyword in joined_quote
    )
    if shipment_header_hits >= 2:
        return True

    # Safety rule for rows parsed like "Tablet / 19143 / 28900" or
    # "TTL / 64762 / 88450", which are volume rows, not products with prices.
    if category == "tft-lcd" and re.search(
        r"^(tablet|notebook|monitor|tv|ttl)\s*/\s*\d+(?:\.\d+)?\s*/\s*\d+",
        product_name,
        flags=re.I,
    ):
        return True

    return False


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

        if is_non_price_section(section_title, headers):
            continue

        first_price_column = find_first_price_column(headers)
        update_time = get_section_update(section)
        section_kind = classify_section(section_title)

        body_rows = table.select("tbody tr")
        rows = body_rows if body_rows else table.find_all("tr")

        for tr in rows:
            if tr.find_parent("thead") or tr.find_parent("tfoot"):
                continue

            cells: list[str] = []
            cell_tags: list[Tag] = []

            for td in tr.find_all("td", recursive=False):
                classes = td.get("class") or []

                if "desktop-only" in classes and "history-cell" in classes:
                    continue

                if td.get("colspan"):
                    continue

                cells.append(clean_text(td))
                cell_tags.append(td)

            if len(cells) < 2:
                continue

            row_headers = headers[: len(cells)]

            if len(cells) > len(headers):
                row_headers += [
                    f"extra_{i}" for i in range(len(headers) + 1, len(cells) + 1)
                ]

            row = dict(zip(row_headers, cells))
            product_parts = [part for part in cells[:first_price_column] if part]
            product_name = " / ".join(product_parts) if product_parts else cells[0]
            product_cells = cell_tags[:first_price_column] if first_price_column > 0 else cell_tags[:1]
            company_names = extract_company_names_from_cells(product_cells, product_name)
            primary_label, primary_value = find_primary_value(row)

            if primary_value is None and not any(
                parse_decimal(value) is not None for value in row.values()
            ):
                continue

            record = {
                "captured_at": captured_at.isoformat(),
                "category": fetch.category,
                "section": section_title,
                "section_kind": section_kind,
                "source_url": fetch.url,
                "source_update": update_time,
                "product_name": product_name,
                "company_names": company_names,
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

    if "records" in data:
        records = data.get("records", [])
        return {record_key(record): record for record in records}

    if "quotes" in data:
        records = data.get("quotes", [])
        converted_records = []

        for quote in records:
            converted_records.append(
                {
                    "category": quote.get("category", ""),
                    "section": quote.get("table", ""),
                    "product_name": quote.get("product_name", ""),
                    "currency": quote.get("currency") or "",
                    "primary_value": quote.get("quote_value"),
                }
            )

        return {record_key(record): record for record in converted_records}

    return {}


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
        lowered = key.lower()

        if key in name_headers or "走勢圖" in key:
            continue

        if any(hint in key or hint in lowered for hint in CHANGE_HEADER_HINTS):
            continue

        if value:
            parts.append(f"{key}: {value}")

    return "; ".join(parts)


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "N/A"

    direction = "▲" if value > 0 else "▼" if value < 0 else "—"
    return f"{direction} {value:.2f}%"


def percent_css_class(value: float | None) -> str:
    if value is None:
        return "percent-none"
    if value > 0:
        return "percent-up"
    if value < 0:
        return "percent-down"
    return "percent-flat"


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
        "> PDF/HTML 報告採用台灣市場慣例：漲幅以紅色標示，跌幅以綠色標示。",
        "> 昨日比較以 repository 中前一次保存的主要報價欄位計算。若前一份資料沒有相同品項，則顯示 N/A。",
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
                f"{product_name_for_report(record)} | "
                f"{quote_summary(record)} | "
                f"來源漲跌幅: {fmt_percent(record.get('site_change_percent'))} | "
                f"昨日比較: {fmt_percent(record.get('daily_change_percent'))}"
                + (f" | 現貨/期貨或合約價差: {spread}" if spread else "")
            )

        lines.append("")

    return "\n".join(lines).strip() + "\n"


def build_html_report(records: list[dict[str, Any]], captured_at: datetime) -> str:
    rows = []

    for record in records:
        spread = record.get("spot_contract_spread", {}).get("label", "")
        source_change = record.get("site_change_percent")
        daily_change = record.get("daily_change_percent")

        rows.append(
            "<tr>"
            f"<td>{html.escape(record['category'])}</td>"
            f"<td>{html.escape(record['section'])}</td>"
            f"<td>{html.escape(product_name_for_report(record))}</td>"
            f"<td>{html.escape(quote_summary(record))}</td>"
            f"<td class=\"{percent_css_class(source_change)}\">{html.escape(fmt_percent(source_change))}</td>"
            f"<td class=\"{percent_css_class(daily_change)}\">{html.escape(fmt_percent(daily_change))}</td>"
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
    @page {{
      size: A4 landscape;
      margin: 12mm;
    }}
    body {{
      font-family: "Noto Sans CJK TC", "Noto Sans CJK", "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #17202a;
      font-size: 12px;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      font-size: 11px;
    }}
    th, td {{
      border: 1px solid #d8dee4;
      padding: 6px;
      vertical-align: top;
      word-break: break-word;
    }}
    th {{
      background: #f6f8fa;
      text-align: left;
    }}
    h1 {{
      font-size: 22px;
      margin-bottom: 4px;
    }}
    .meta {{
      color: #57606a;
      margin-top: 0;
    }}
    .note {{
      color: #57606a;
      font-size: 11px;
      margin: 4px 0 12px;
    }}
    .percent-up {{
      color: #d1242f;
      font-weight: 700;
    }}
    .percent-down {{
      color: #1a7f37;
      font-weight: 700;
    }}
    .percent-flat {{
      color: #57606a;
      font-weight: 600;
    }}
    .percent-none {{
      color: #8c959f;
    }}
  </style>
</head>
<body>
  <h1>TrendForce 每日報價報告 - {captured_at:%Y-%m-%d}</h1>
  <p class="meta">抓取時間：{captured_at:%Y-%m-%d %H:%M:%S %Z}</p>
  <p class="note">顏色標示採台灣市場慣例：漲幅為紅色，跌幅為綠色。</p>
  <table>
    <thead>
      <tr>
        <th>分類</th>
        <th>產品群組</th>
        <th>產品名稱</th>
        <th>報價資料</th>
        <th>來源漲跌幅</th>
        <th>昨日比較</th>
        <th>現貨/期貨或合約價差</th>
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


def write_pdf_report(path: Path, html_content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_content, base_url=str(Path.cwd())).write_pdf(str(path))


def smtp_config_missing() -> list[str]:
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    mail_from = os.environ.get("SMTP_FROM") or os.environ.get("MAIL_FROM") or smtp_user

    missing = [
        name
        for name, value in {
            "SMTP_HOST": smtp_host,
            "SMTP_USERNAME": smtp_user,
            "SMTP_PASSWORD": smtp_password,
            "SMTP_FROM/MAIL_FROM/SMTP_USERNAME": mail_from,
        }.items()
        if not value
    ]

    return missing


def send_email(
    subject: str,
    text_body: str,
    html_body: str,
    recipient: str,
    attachment_path: Path | None = None,
) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    mail_from = os.environ.get("SMTP_FROM") or os.environ.get("MAIL_FROM") or smtp_user
    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if attachment_path and attachment_path.exists():
        suffix = attachment_path.suffix.lower()

        if suffix == ".pdf":
            message.add_attachment(
                attachment_path.read_bytes(),
                maintype="application",
                subtype="pdf",
                filename=attachment_path.name,
            )
        elif suffix == ".html":
            message.add_attachment(
                attachment_path.read_bytes(),
                maintype="text",
                subtype="html",
                filename=attachment_path.name,
            )
        else:
            message.add_attachment(
                attachment_path.read_bytes(),
                maintype="text",
                subtype="plain",
                filename=attachment_path.name,
            )

    if use_ssl:
        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            timeout=30,
            context=ssl.create_default_context(),
        ) as smtp:
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)


def run(args: argparse.Namespace) -> int:
    captured_at = datetime.now(TIMEZONE)
    today = captured_at.date()

    output_dir = Path(args.output_dir)
    history_dir = Path(args.history_dir)
    history_path = Path(args.history)

    report_html_path = Path(args.report_html) if args.report_html else output_dir / "latest.html"
    report_md_path = Path(args.report_md) if args.report_md else output_dir / "latest.md"
    report_pdf_path = Path(args.report_pdf) if args.report_pdf else output_dir / "latest.pdf"

    daily_report_md_path = output_dir / f"trendforce_daily_report_{today}.md"
    daily_report_pdf_path = output_dir / f"trendforce_daily_report_{today}.pdf"
    daily_snapshot_path = history_dir / f"{today}.json"

    previous_records = load_previous_records(history_path)

    if not previous_records:
        previous_daily_snapshot = history_dir / f"{today - timedelta(days=1)}.json"
        previous_records = load_previous_records(previous_daily_snapshot)

    records: list[dict[str, Any]] = []

    for fetch in fetch_pages(timeout=args.timeout):
        records.extend(parse_price_page(fetch, captured_at))

    # Final guard: keep only actual price quote records. This removes shipment
    # statistics such as TFT-LCD "Large Size Panel Shipment $USD" from the report.
    records = [record for record in records if not is_non_price_record(record)]

    if not records:
        raise RuntimeError("No price records were parsed from TrendForce pages.")

    add_daily_comparisons(records, previous_records)
    add_spreads(records)

    html_report = build_html_report(records, captured_at)
    markdown_report = build_markdown_report(records, captured_at)

    write_report(report_html_path, html_report)
    write_report(report_md_path, markdown_report)
    write_report(daily_report_md_path, markdown_report)

    write_pdf_report(report_pdf_path, html_report)
    write_pdf_report(daily_report_pdf_path, html_report)

    save_history(daily_snapshot_path, records, captured_at)

    if args.save_history:
        save_history(history_path, records, captured_at)

    if args.send_email:
        missing = smtp_config_missing()

        if missing and args.skip_email_if_missing_secrets:
            print(
                f"Skip email because missing SMTP configuration: {', '.join(missing)}",
                file=sys.stderr,
            )
        elif missing:
            raise RuntimeError(f"Missing email configuration: {', '.join(missing)}")
        else:
            recipient = (
                args.mail_to
                or args.recipient
                or os.environ.get("MAIL_TO")
                or os.environ.get("REPORT_RECIPIENT")
                or DEFAULT_MAIL_TO
            )

            send_email(
                subject=f"TrendForce 每日報價報告 {today}",
                text_body=markdown_report,
                html_body=html_report,
                recipient=recipient,
                attachment_path=daily_report_pdf_path,
            )

    print(f"Parsed {len(records)} records.")
    print(f"HTML report: {report_html_path}")
    print(f"Markdown report: {report_md_path}")
    print(f"PDF report: {report_pdf_path}")
    print(f"Daily Markdown report: {daily_report_md_path}")
    print(f"Daily PDF report: {daily_report_pdf_path}")
    print(f"Daily snapshot: {daily_snapshot_path}")

    if args.save_history:
        print(f"History: {history_path}")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily TrendForce price report.")

    parser.add_argument("--history", default="data/trendforce_history.json")
    parser.add_argument("--history-dir", default="data/history")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--report-html", default=None)
    parser.add_argument("--report-md", default=None)
    parser.add_argument("--report-pdf", default=None)
    parser.add_argument("--mail-to", default=None)
    parser.add_argument("--recipient", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--save-history", action="store_true")
    parser.add_argument("--send-email", action="store_true")
    parser.add_argument("--skip-email-if-missing-secrets", action="store_true")

    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
