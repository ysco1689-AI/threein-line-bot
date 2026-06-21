import re
from datetime import datetime, timedelta, timezone


def safe_filename_part(value, fallback="unknown"):
    text = str(value or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:50] or fallback

def normalize_date_text(value):
    text = str(value).strip()
    if not text:
        return ""

    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue

    return text

def parse_date(value):
    normalized = normalize_date_text(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%Y/%m/%d").date()
    except ValueError:
        return None

def today_text():
    taipei_tz = timezone(timedelta(hours=8))
    return datetime.now(taipei_tz).strftime("%Y/%m/%d")

def now_time_text():
    taipei_tz = timezone(timedelta(hours=8))
    return datetime.now(taipei_tz).strftime("%H:%M")

def get_records_by_header(sheet, required_header):
    values = sheet.get_all_values()

    for header_index, row in enumerate(values):
        headers = [str(cell).strip() for cell in row]
        if required_header not in headers:
            continue

        records = []
        for data_row in values[header_index + 1:]:
            if not any(str(cell).strip() for cell in data_row):
                continue

            record = {}
            for col_index, header in enumerate(headers):
                if not header:
                    continue
                record[header] = data_row[col_index] if col_index < len(data_row) else ""
            records.append(record)

        return records

    return []

def normalize_text(text):
    return str(text).upper().replace(" ", "").replace("　", "").strip()
