import os
import json
import tempfile
import re
from datetime import date, datetime, timedelta, timezone

import google.generativeai as genai
import gspread

from google.oauth2.service_account import Credentials
from flask import Flask, request, send_file

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction
)
from linebot.v3.webhooks import (
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
    ImageMessageContent,
    VideoMessageContent
)

app = Flask(__name__)

recipe_cache = []
users_cache = []
qa_cache = []
user_states = {}

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHIFT_SPREADSHEET_ID = os.getenv("SHIFT_SPREADSHEET_ID", SPREADSHEET_ID)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


def get_google_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )
    return gspread.authorize(credentials)


def load_all_data():
    global recipe_cache, users_cache, qa_cache

    try:
        gc = get_google_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        try:
            recipe_cache = spreadsheet.sheet1.get_all_records()
        except Exception as e:
            recipe_cache = []
            print("配方資料載入失敗，暫時略過：", e)

        try:
            users_cache = spreadsheet.worksheet("users").get_all_records()
        except Exception as e:
            users_cache = []
            print("users 工作表載入失敗，暫時略過：", e)

        try:
            qa_cache = spreadsheet.worksheet("qa").get_all_records()
        except Exception as e:
            qa_cache = []
            print("qa 工作表載入失敗，暫時略過：", e)

        print("Google Sheet 資料載入成功")

    except Exception as e:
        print("讀取 Google Sheet 失敗：", e)


def add_new_user(user_id):
    global users_cache

    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("users")

    sheet.append_row([
        user_id,
        "新使用者",
        "guest",
        "pending",
        "自動加入"
    ])

    users_cache.append({
        "line_user_id": user_id,
        "name": "新使用者",
        "role": "guest",
        "status": "pending",
        "note": "自動加入"
    })

    print("已自動新增使用者:", user_id)


def get_user_role(user_id):
    for user in users_cache:
        if str(user.get("line_user_id", "")).strip() == user_id:
            return {
                "role": str(user.get("role", "")).strip(),
                "status": str(user.get("status", "")).strip()
            }

    add_new_user(user_id)

    return {
        "role": "guest",
        "status": "pending"
    }


def reply_to_line(event, reply_text, quick_reply=None):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            message = TextMessage(text=reply_text, quick_reply=quick_reply)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[message]
                )
            )
    except Exception as e:
        print(f"[ERROR] reply_to_line 失敗: {e}")


def get_spreadsheet():
    gc = get_google_client()
    return gc.open_by_key(SPREADSHEET_ID)


def get_shift_spreadsheet():
    gc = get_google_client()
    return gc.open_by_key(SHIFT_SPREADSHEET_ID)


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


def find_accessible_shift(user_id):
    today = parse_date(today_text())
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("排班表")
    rows = get_records_by_header(sheet, "LINE_ID")

    for row in rows:
        line_id = str(row.get("LINE_ID", "")).strip()
        status = str(row.get("狀態", "啟用")).strip()
        start_date = parse_date(row.get("開始日期", ""))
        end_date = parse_date(row.get("結束日期", ""))

        if line_id != user_id:
            continue

        if status and status not in ["啟用", "active", "Active", "ACTIVE"]:
            continue

        if not start_date or not end_date:
            continue

        if start_date <= today <= end_date + timedelta(days=1):
            return {
                "line_id": line_id,
                "name": str(row.get("姓名", "")).strip(),
                "shift_name": str(row.get("檔期名稱", "")).strip(),
                "booth": str(row.get("攤位編號", "")).strip(),
                "start_date": start_date.strftime("%Y/%m/%d"),
                "end_date": end_date.strftime("%Y/%m/%d")
            }

    return None


def find_today_shift(user_id):
    return find_accessible_shift(user_id)


def write_shift_confirmation(user_id, shift):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("確認紀錄")
    sheet.append_row([
        today_text(),
        user_id,
        shift.get("name", ""),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        now_time_text(),
        "已確認",
        ""
    ])


def confirm_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✅ 確認", text="✅ 確認")),
        QuickReplyItem(action=MessageAction(label="❌ 不是我的", text="❌ 不是我的"))
    ])


def start_confirm_shift_flow(event, user_id):
    try:
        shift = find_today_shift(user_id)
    except Exception as e:
        print("[ERROR] 查詢排班表失敗:", e)
        reply_to_line(event, "查詢排班表時發生問題，請稍後再試或聯絡主管。")
        return

    if not shift:
        user_states.pop(user_id, None)
        reply_to_line(event, "查無您今日的排班紀錄，請聯絡主管確認。")
        return

    user_states[user_id] = {
        "flow": "confirm_shift",
        "step": "waiting_confirm",
        "data": shift
    }

    reply_to_line(
        event,
        "您今日的檔期如下，請確認：\n"
        f"檔期名稱：{shift['shift_name']}\n"
        f"攤位：{shift['booth']}\n"
        "是否確認？",
        quick_reply=confirm_quick_reply()
    )


def mark_shift_confirmed(user_id, shift):
    user_states[user_id] = {
        "shift_confirmed": True,
        "confirmed_date": today_text(),
        "data": shift
    }


def check_shift_confirmed(user_id):
    state = user_states.get(user_id, {})
    if (
        state.get("shift_confirmed") is True
        and state.get("confirmed_date") == today_text()
    ):
        return True

    try:
        spreadsheet = get_shift_spreadsheet()
        sheet = spreadsheet.worksheet("確認紀錄")
        rows = get_records_by_header(sheet, "LINE_ID")
        for row in reversed(rows):
            if (
                str(row.get("LINE_ID", "")).strip() == user_id
                and normalize_date_text(row.get("日期", "")) == today_text()
                and str(row.get("確認狀態", "已確認")).strip() == "已確認"
            ):
                shift = find_accessible_shift(user_id)
                if shift:
                    mark_shift_confirmed(user_id, shift)
                    return True
    except Exception as e:
        print("[ERROR] 讀取今日確認狀態失敗:", e)

    return False


def require_shift_confirmed(event, user_id):
    if check_shift_confirmed(user_id):
        return True

    reply_to_line(event, "請先點選「✅ 確認檔期」完成今日確認後，才能使用此功能。")
    return False


def get_confirmed_shift(user_id):
    state = user_states.get(user_id, {})
    shift = state.get("data")
    if shift:
        return shift

    shift = find_accessible_shift(user_id)
    if shift:
        state["data"] = shift
        user_states[user_id] = state
    return shift


def date_quick_reply(dates):
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(
                label=value.strftime("%m/%d"),
                text=f"杯數日期 {value.strftime('%Y/%m/%d')}"
            )
        )
        for value in dates[:13]
    ])


def cup_update_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="✅ 確認修改", text="✅ 確認修改")
        ),
        QuickReplyItem(
            action=MessageAction(label="❌ 取消", text="❌ 取消修改")
        )
    ])


def available_cup_dates(shift):
    start_date = parse_date(shift.get("start_date"))
    end_date = parse_date(shift.get("end_date"))
    today = parse_date(today_text())
    if not start_date or not end_date or not today:
        return []

    last_available_date = min(end_date, today)
    if last_available_date < start_date:
        return []

    dates = []
    current = start_date
    while current <= last_available_date:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def start_cup_report_flow(event, user_id):
    shift = get_confirmed_shift(user_id)
    if not shift:
        reply_to_line(event, "找不到目前可回報的檔期，請聯絡主管確認排班。")
        return

    dates = available_cup_dates(shift)
    if not dates:
        reply_to_line(event, "目前沒有可填寫的檔期日期。")
        return

    state = user_states.get(user_id, {})
    state.update({
        "flow": "report_cups",
        "step": "waiting_cup_date",
        "data": shift,
        "cup_report": {}
    })
    user_states[user_id] = state

    date_text = " / ".join(value.strftime("%m/%d") for value in dates)
    reply_to_line(
        event,
        f"🥤 請選擇要填寫的日期：\n{date_text}",
        quick_reply=date_quick_reply(dates)
    )


def find_cup_record(user_id, shift_name, report_date):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("杯數回報")
    values = sheet.get_all_values()

    header_index = None
    headers = []
    for index, row in enumerate(values):
        row_headers = [str(cell).strip() for cell in row]
        if "LINE_ID" in row_headers and "杯數" in row_headers:
            header_index = index
            headers = row_headers
            break

    if header_index is None:
        return None

    for row_index in range(header_index + 1, len(values)):
        row = values[row_index]
        record = {
            header: row[col_index] if col_index < len(row) else ""
            for col_index, header in enumerate(headers)
            if header
        }
        if (
            normalize_date_text(record.get("日期", "")) == report_date
            and str(record.get("LINE_ID", "")).strip() == user_id
            and str(record.get("檔期名稱", "")).strip() == shift_name
        ):
            record["_row_number"] = row_index + 1
            return record

    return None


def write_new_cup_record(user_id, shift, report_date, cups):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("杯數回報")
    sheet.append_row([
        report_date,
        user_id,
        shift.get("name", ""),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        cups,
        now_time_text(),
        "否",
        "",
        ""
    ])


def update_cup_record(existing_record, cups):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("杯數回報")
    row_number = existing_record["_row_number"]
    original_cups = str(existing_record.get("杯數", "")).strip()
    sheet.update(
        range_name=f"F{row_number}:J{row_number}",
        values=[[
            cups,
            existing_record.get("填寫時間", "") or now_time_text(),
            "是",
            original_cups,
            now_time_text()
        ]]
    )


def finish_cup_flow(user_id):
    state = user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("cup_report", None)
    user_states[user_id] = state


def handle_cup_report_text(event, user_id, user_message):
    state = user_states.get(user_id, {})
    step = state.get("step")
    shift = state.get("data", {})
    cup_report = state.setdefault("cup_report", {})
    message = user_message.strip()

    if step == "waiting_cup_date":
        match = re.fullmatch(r"杯數日期\s+(\d{4}/\d{2}/\d{2})", message)
        selected_date = match.group(1) if match else normalize_date_text(message)
        allowed_dates = {
            value.strftime("%Y/%m/%d")
            for value in available_cup_dates(shift)
        }

        if selected_date not in allowed_dates:
            reply_to_line(
                event,
                "請使用下方按鈕選擇檔期日期。",
                quick_reply=date_quick_reply(available_cup_dates(shift))
            )
            return True

        cup_report["date"] = selected_date
        state["step"] = "waiting_cup_count"
        user_states[user_id] = state
        reply_to_line(
            event,
            f"請輸入 {selected_date} 的杯數（正整數）。\n"
            "照片辨識會在第五階段開放，目前請直接輸入數字。"
        )
        return True

    if step == "waiting_cup_count":
        if not re.fullmatch(r"[1-9]\d*", message):
            reply_to_line(event, "杯數必須是大於 0 的整數，請重新輸入。")
            return True

        cups = int(message)
        report_date = cup_report.get("date")
        try:
            existing = find_cup_record(
                user_id,
                shift.get("shift_name", ""),
                report_date
            )
        except Exception as e:
            print("[ERROR] 查詢杯數紀錄失敗:", e)
            reply_to_line(event, "查詢既有杯數資料時發生問題，請稍後再試。")
            return True

        if not existing:
            try:
                write_new_cup_record(user_id, shift, report_date, cups)
            except Exception as e:
                print("[ERROR] 寫入杯數紀錄失敗:", e)
                reply_to_line(event, "寫入杯數資料時發生問題，請稍後再試。")
                return True

            finish_cup_flow(user_id)
            reply_to_line(event, f"🥤 {report_date} 杯數回報完成：{cups} 杯")
            return True

        existing_cups = int(str(existing.get("杯數", "0")).strip() or 0)
        if existing_cups == cups:
            finish_cup_flow(user_id)
            reply_to_line(
                event,
                f"此日杯數已填寫（{existing_cups} 杯），資料相同無需重複填寫。"
            )
            return True

        cup_report["proposed_cups"] = cups
        cup_report["existing_record"] = existing
        state["step"] = "waiting_cup_update_confirm"
        user_states[user_id] = state
        reply_to_line(
            event,
            f"⚠️ 您 {report_date} 已填寫過杯數：{existing_cups} 杯\n"
            f"您現在輸入：{cups} 杯\n"
            "數字不同，確認要修改嗎？",
            quick_reply=cup_update_quick_reply()
        )
        return True

    if step == "waiting_cup_update_confirm":
        if message in ["✅ 確認修改", "確認修改"]:
            existing = cup_report.get("existing_record")
            cups = cup_report.get("proposed_cups")
            report_date = cup_report.get("date")
            try:
                update_cup_record(existing, cups)
            except Exception as e:
                print("[ERROR] 修改杯數紀錄失敗:", e)
                reply_to_line(event, "修改杯數資料時發生問題，請稍後再試。")
                return True

            finish_cup_flow(user_id)
            reply_to_line(event, f"🥤 {report_date} 杯數已修改為：{cups} 杯")
            return True

        if message in ["❌ 取消修改", "取消修改", "取消"]:
            finish_cup_flow(user_id)
            reply_to_line(event, "已取消修改，原杯數資料保持不變。")
            return True

        reply_to_line(
            event,
            "請選擇「✅ 確認修改」或「❌ 取消」。",
            quick_reply=cup_update_quick_reply()
        )
        return True

    return False


def drive_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="🚗 有開車", text="🚗 有開車")
        ),
        QuickReplyItem(
            action=MessageAction(label="🚶 沒有開車", text="🚶 沒有開車")
        )
    ])


def mileage_manage_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="➕ 新增車輛", text="➕ 新增車輛")
        ),
        QuickReplyItem(
            action=MessageAction(label="✏️ 修改紀錄", text="✏️ 修改紀錄")
        )
    ])


def mileage_plate_quick_reply(records):
    items = []
    for record in records[:13]:
        plate = str(record.get("車牌尾四碼", "")).strip()
        if plate:
            items.append(
                QuickReplyItem(
                    action=MessageAction(
                        label=plate,
                        text=f"修改車輛 {plate}"
                    )
                )
            )
    return QuickReply(items=items) if items else None


def find_today_mileage_records(user_id):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("里程回報")
    values = sheet.get_all_values()
    header_index = None
    headers = []
    for index, row in enumerate(values):
        row_headers = [str(cell).strip() for cell in row]
        if "LINE_ID" in row_headers and "是否開車" in row_headers:
            header_index = index
            headers = row_headers
            break

    if header_index is None:
        return []

    records = []
    for row_index in range(len(values) - 1, header_index, -1):
        row = values[row_index]
        record = {
            header: row[col_index] if col_index < len(row) else ""
            for col_index, header in enumerate(headers)
            if header
        }
        if (
            str(record.get("LINE_ID", "")).strip() == user_id
            and normalize_date_text(record.get("日期", "")) == today_text()
        ):
            record["_row_number"] = row_index + 1
            records.append(record)
    return records


def write_mileage_record(
    user_id,
    shift,
    drove,
    plate_last_four="",
    start_mileage="",
    end_mileage="",
    distance=""
):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("里程回報")
    sheet.append_row([
        today_text(),
        user_id,
        shift.get("name", ""),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        "是" if drove else "否",
        plate_last_four,
        start_mileage,
        end_mileage,
        distance,
        now_time_text()
    ])


def start_mileage_report_flow(event, user_id):
    shift = get_confirmed_shift(user_id)
    if not shift:
        reply_to_line(event, "找不到目前可回報的檔期，請聯絡主管確認排班。")
        return

    try:
        records = find_today_mileage_records(user_id)
    except Exception as e:
        print("[ERROR] 查詢里程紀錄失敗:", e)
        reply_to_line(event, "查詢里程資料時發生問題，請稍後再試。")
        return

    missing_plate_record = next(
        (
            record for record in records
            if str(record.get("是否開車", "")).strip() == "是"
            and not str(record.get("車牌尾四碼", "")).strip()
        ),
        None
    )
    if missing_plate_record:
        state = user_states.get(user_id, {})
        state.update({
            "flow": "report_mileage",
            "step": "waiting_existing_plate_last_four",
            "data": shift,
            "mileage_report": {"existing_record": missing_plate_record}
        })
        user_states[user_id] = state
        reply_to_line(
            event,
            "您今日有一筆舊里程紀錄尚未填寫車牌。\n"
            "請先補輸入車牌尾四碼，例如：1234 或 AB12"
        )
        return

    if records:
        drove_records = [
            record for record in records
            if str(record.get("是否開車", "")).strip() == "是"
        ]
        summary_lines = []
        for record in reversed(drove_records):
            plate = str(record.get("車牌尾四碼", "")).strip()
            distance = str(record.get("本次里程數", "")).strip()
            summary_lines.append(f"車牌 {plate}：{distance or '0'} 公里")

        summary = "\n".join(summary_lines) if summary_lines else "目前紀錄：沒有開車"
        state = user_states.get(user_id, {})
        state.update({
            "flow": "report_mileage",
            "step": "waiting_mileage_action",
            "data": shift,
            "mileage_report": {"today_records": records}
        })
        user_states[user_id] = state
        reply_to_line(
            event,
            f"今日里程紀錄：\n{summary}\n\n請選擇操作：",
            quick_reply=mileage_manage_quick_reply()
        )
        return

    state = user_states.get(user_id, {})
    state.update({
        "flow": "report_mileage",
        "step": "waiting_drive_status",
        "data": shift,
        "mileage_report": {}
    })
    user_states[user_id] = state
    reply_to_line(
        event,
        "今日是否有開車前往？",
        quick_reply=drive_quick_reply()
    )


def finish_mileage_flow(user_id):
    state = user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("mileage_report", None)
    user_states[user_id] = state


def parse_mileage_number(value):
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return None
    number = float(text)
    if number < 0:
        return None
    return number


def normalize_plate_last_four(value):
    text = re.sub(r"[\s-]+", "", str(value)).upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", text):
        return None
    return text


def update_mileage_plate(existing_record, plate_last_four):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("里程回報")
    sheet.update_acell(
        f"G{existing_record['_row_number']}",
        plate_last_four
    )


def update_mileage_record(
    existing_record,
    plate_last_four,
    start_mileage,
    end_mileage,
    distance
):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("里程回報")
    row_number = existing_record["_row_number"]
    sheet.update(
        range_name=f"F{row_number}:K{row_number}",
        values=[[
            "是",
            plate_last_four,
            start_mileage,
            end_mileage,
            distance,
            now_time_text()
        ]]
    )


def format_mileage_number(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def handle_mileage_report_text(event, user_id, user_message):
    state = user_states.get(user_id, {})
    step = state.get("step")
    shift = state.get("data", {})
    mileage_report = state.setdefault("mileage_report", {})
    message = user_message.strip()

    if step == "waiting_mileage_action":
        records = mileage_report.get("today_records", [])
        if message in ["➕ 新增車輛", "新增車輛", "新增"]:
            no_drive_record = next(
                (
                    record for record in records
                    if str(record.get("是否開車", "")).strip() == "否"
                ),
                None
            )
            mileage_report.clear()
            mileage_report["mode"] = "add"
            if no_drive_record:
                mileage_report["replace_record"] = no_drive_record
            state["step"] = "waiting_plate_last_four"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請輸入新增車輛的車牌尾四碼。\n例如：1234 或 AB12"
            )
            return True

        if message in ["✏️ 修改紀錄", "修改紀錄", "修改"]:
            drove_records = [
                record for record in records
                if str(record.get("是否開車", "")).strip() == "是"
                and str(record.get("車牌尾四碼", "")).strip()
            ]
            if not drove_records:
                reply_to_line(
                    event,
                    "今日沒有可修改的車輛紀錄，請選擇新增車輛。",
                    quick_reply=mileage_manage_quick_reply()
                )
                return True

            state["step"] = "waiting_modify_vehicle"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇要修改的車牌：",
                quick_reply=mileage_plate_quick_reply(drove_records)
            )
            return True

        reply_to_line(
            event,
            "請選擇「➕ 新增車輛」或「✏️ 修改紀錄」。",
            quick_reply=mileage_manage_quick_reply()
        )
        return True

    if step == "waiting_modify_vehicle":
        match = re.fullmatch(r"修改車輛\s+([A-Za-z0-9]{4})", message)
        plate = normalize_plate_last_four(match.group(1)) if match else None
        records = mileage_report.get("today_records", [])
        existing_record = next(
            (
                record for record in records
                if str(record.get("車牌尾四碼", "")).strip().upper() == plate
            ),
            None
        )
        if not existing_record:
            drove_records = [
                record for record in records
                if str(record.get("是否開車", "")).strip() == "是"
            ]
            reply_to_line(
                event,
                "請使用下方按鈕選擇要修改的車牌。",
                quick_reply=mileage_plate_quick_reply(drove_records)
            )
            return True

        mileage_report.clear()
        mileage_report.update({
            "mode": "update",
            "existing_record": existing_record,
            "plate_last_four": plate
        })
        state["step"] = "waiting_start_mileage"
        user_states[user_id] = state
        reply_to_line(
            event,
            f"正在修改車牌 {plate}。\n請輸入新的出發里程。"
        )
        return True

    if step == "waiting_existing_plate_last_four":
        plate_last_four = normalize_plate_last_four(message)
        if plate_last_four is None:
            reply_to_line(
                event,
                "車牌尾四碼必須是 4 個英文字母或數字，例如：1234 或 AB12。"
            )
            return True

        try:
            update_mileage_plate(
                mileage_report.get("existing_record", {}),
                plate_last_four
            )
        except Exception as e:
            print("[ERROR] 補填車牌尾四碼失敗:", e)
            reply_to_line(event, "補填車牌時發生問題，請稍後再試。")
            return True

        finish_mileage_flow(user_id)
        reply_to_line(event, f"🚗 車牌尾四碼已補填完成：{plate_last_four}")
        return True

    if step == "waiting_drive_status":
        if message in ["🚶 沒有開車", "沒有開車", "沒開車"]:
            try:
                write_mileage_record(user_id, shift, False)
            except Exception as e:
                print("[ERROR] 寫入無里程紀錄失敗:", e)
                reply_to_line(event, "寫入里程資料時發生問題，請稍後再試。")
                return True

            finish_mileage_flow(user_id)
            reply_to_line(event, "🚶 已記錄今日沒有開車，里程回報完成。")
            return True

        if message in ["🚗 有開車", "有開車", "開車"]:
            state["step"] = "waiting_plate_last_four"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請輸入本次使用車輛的車牌尾四碼。\n"
                "例如：1234 或 AB12"
            )
            return True

        reply_to_line(
            event,
            "請選擇「🚗 有開車」或「🚶 沒有開車」。",
            quick_reply=drive_quick_reply()
        )
        return True

    if step == "waiting_plate_last_four":
        plate_last_four = normalize_plate_last_four(message)
        if plate_last_four is None:
            reply_to_line(
                event,
                "車牌尾四碼必須是 4 個英文字母或數字，例如：1234 或 AB12。"
            )
            return True

        try:
            today_records = find_today_mileage_records(user_id)
        except Exception as e:
            print("[ERROR] 查詢車牌紀錄失敗:", e)
            reply_to_line(event, "查詢車牌紀錄時發生問題，請稍後再試。")
            return True

        duplicate_record = next(
            (
                record for record in today_records
                if str(record.get("是否開車", "")).strip() == "是"
                and str(record.get("車牌尾四碼", "")).strip().upper() == plate_last_four
            ),
            None
        )
        if duplicate_record:
            mileage_report.clear()
            mileage_report.update({
                "mode": "update",
                "existing_record": duplicate_record,
                "plate_last_four": plate_last_four
            })
            state["step"] = "waiting_start_mileage"
            user_states[user_id] = state
            reply_to_line(
                event,
                f"車牌 {plate_last_four} 今日已有紀錄，已切換為修改模式。\n"
                "請輸入新的出發里程。"
            )
            return True

        mileage_report["plate_last_four"] = plate_last_four
        mileage_report.setdefault("mode", "add")
        state["step"] = "waiting_start_mileage"
        user_states[user_id] = state
        reply_to_line(event, "請輸入出發里程（儀表板公里數）。")
        return True

    if step == "waiting_start_mileage":
        start_mileage = parse_mileage_number(message)
        if start_mileage is None:
            reply_to_line(event, "出發里程必須是 0 以上的數字，請重新輸入。")
            return True

        mileage_report["start_mileage"] = start_mileage
        state["step"] = "waiting_end_mileage"
        user_states[user_id] = state
        reply_to_line(event, "請輸入收攤里程（儀表板公里數）。")
        return True

    if step == "waiting_end_mileage":
        end_mileage = parse_mileage_number(message)
        if end_mileage is None:
            reply_to_line(event, "收攤里程必須是 0 以上的數字，請重新輸入。")
            return True

        start_mileage = mileage_report.get("start_mileage")
        if end_mileage <= start_mileage:
            reply_to_line(
                event,
                f"收攤里程必須大於出發里程（{format_mileage_number(start_mileage)}），請重新輸入。"
            )
            return True

        distance = round(end_mileage - start_mileage, 2)
        start_text = format_mileage_number(start_mileage)
        end_text = format_mileage_number(end_mileage)
        distance_text = format_mileage_number(distance)

        try:
            existing_record = (
                mileage_report.get("existing_record")
                or mileage_report.get("replace_record")
            )
            if existing_record:
                update_mileage_record(
                    existing_record,
                    mileage_report.get("plate_last_four", ""),
                    start_text,
                    end_text,
                    distance_text
                )
            else:
                write_mileage_record(
                    user_id,
                    shift,
                    True,
                    plate_last_four=mileage_report.get("plate_last_four", ""),
                    start_mileage=start_text,
                    end_mileage=end_text,
                    distance=distance_text
                )
        except Exception as e:
            print("[ERROR] 寫入里程紀錄失敗:", e)
            reply_to_line(event, "寫入里程資料時發生問題，請稍後再試。")
            return True

        finish_mileage_flow(user_id)
        reply_to_line(
            event,
            "🚗 里程回報完成！\n"
            f"車牌尾四碼：{mileage_report.get('plate_last_four', '')}\n"
            f"本次行駛：{distance_text} 公里"
        )
        return True

    return False


def handle_confirm_shift_text(event, user_id, user_message):
    state = user_states.get(user_id, {})
    shift = state.get("data", {})
    message = user_message.strip()

    if message in ["✅ 確認", "確認"]:
        try:
            write_shift_confirmation(user_id, shift)
        except Exception as e:
            print("[ERROR] 寫入確認紀錄失敗:", e)
            reply_to_line(event, "寫入確認紀錄時發生問題，請稍後再試或聯絡主管。")
            return True

        mark_shift_confirmed(user_id, shift)
        reply_to_line(event, "✅ 確認完成！杯數回報、餘料回報、費用支出、里程回報已開放使用。")
        return True

    if message in ["❌ 不是我的", "不是我的"]:
        user_states.pop(user_id, None)
        reply_to_line(event, "已通知主管，請等候聯繫。如有緊急狀況請直接致電。")
        return True

    reply_to_line(event, "請選擇「✅ 確認」或「❌ 不是我的」。", quick_reply=confirm_quick_reply())
    return True


def handle_active_flow(event, user_id, user_message):
    state = user_states.get(user_id)
    if not state or not state.get("flow"):
        return False

    if state.get("flow") == "confirm_shift" and state.get("step") == "waiting_confirm":
        return handle_confirm_shift_text(event, user_id, user_message)

    if state.get("flow") == "report_cups":
        return handle_cup_report_text(event, user_id, user_message)

    if state.get("flow") == "report_mileage":
        return handle_mileage_report_text(event, user_id, user_message)

    return False


def normalize_text(text):
    return str(text).upper().replace(" ", "").replace("　", "").strip()


def classify_message(user_message):
    message = normalize_text(user_message)

    recipe_keywords = [
        "配方", "比例", "幾克", "幾公克", "多少克",
        "原物料", "成本", "毛利", "煮法", "熬煮",
        "仙草汁", "黑糖", "二砂", "冬瓜糖", "甘草",
        "海鹽", "茶包"
    ]

    food_quality_keywords = [
        "壞掉", "酸掉", "發酸", "怪味", "異味",
        "出水", "太軟", "變色", "發霉", "異物",
        "能不能賣", "可不可以賣",
        "茶湯混濁", "混濁", "沉澱", "結塊"
    ]

    food_quality_context_keywords = ["仙草凍", "茶湯", "糖水", "原料", "飲品", "仙草"]

    qa_keywords = [
        "封口機", "錯誤", "錯誤代碼", "卡膜", "封膜",
        "封不起來", "封不緊", "漏杯", "杯膜", "機器",
        "E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E09",
        "客訴", "太甜", "太淡", "太苦", "沒味道", "味道不對"
    ]

    if any(word in user_message for word in recipe_keywords):
        return "recipe"

    if any(word in user_message for word in food_quality_keywords):
        return "food_quality"

    if "正常嗎" in user_message and any(word in user_message for word in food_quality_context_keywords):
        return "food_quality"

    if "仙草凍正常嗎" in user_message:
        return "food_quality"

    if any(word in user_message for word in qa_keywords):
        return "qa"

    if re.search(r"E\d{2}", message):
        return "qa"

    return "general"


def find_recipe(user_message):
    records = recipe_cache

    want_hot = any(word in user_message for word in ["熱", "熱的", "熱飲", "溫的"])
    want_cold = any(word in user_message for word in ["冷", "冷的", "冷飲", "冰", "冰的"])

    matched_rows = []

    for row in records:
        product_name = str(row.get("品項", "")).strip()

        if product_name and product_name in user_message:
            matched_rows.append(row)

    if not matched_rows:
        return None

    if want_hot:
        for row in matched_rows:
            temp = str(row.get("溫度", "")).strip()
            if temp in ["熱", "熱飲", "溫"]:
                return row

    if want_cold:
        for row in matched_rows:
            temp = str(row.get("溫度", "")).strip()
            if temp in ["冷", "冷飲", "冰"]:
                return row

    for row in matched_rows:
        temp = str(row.get("溫度", "")).strip()
        if temp in ["冷", "冷飲", "冰"]:
            return row

    return matched_rows[0]


def find_qa_answer(user_message, role, status):
    user_msg_norm = normalize_text(user_message)

    for row in qa_cache:
        keywords = str(row.get("keywords", "")).split(",")
        answer = str(row.get("answer", "")).strip()
        permission = str(row.get("permission", "")).strip()

        if len(answer) < 20:
            continue

        matched = False

        for keyword in keywords:
            kw = normalize_text(keyword)

            if not kw:
                continue

            if re.fullmatch(r"E\d{2}", kw):
                if kw in user_msg_norm:
                    matched = True
                    break

            elif len(kw) >= 3 and kw in user_msg_norm:
                matched = True
                break

        if not matched:
            continue

        if permission == "public":
            return answer

        if permission == "franchisee":
            if status == "active" and role in ["admin", "franchisee", "staff"]:
                return answer
            return "此問題需要加盟主或員工權限，請洽總部。"

        if permission == "admin":
            if status == "active" and role == "admin":
                return answer
            return "此問題需要總部權限，請洽總部。"

    return None


def ask_gemini_text(user_message, msg_type):
    if msg_type == "food_quality":
        prompt = f"""你是三入好棧的資深店長，有十年飲料店現場經驗。

員工正在詢問食品或飲品品管問題，請用繁體中文直接回答。

回答風格：
- 像LINE訊息，口氣直接像資深店長
- 先講「現在馬上做什麼」
- 給具體判斷標準與處理步驟
- 安全優先：疑似變質、異味、發霉、異物，一律先暫停販售
- 說明什麼情況可以繼續賣、什麼情況要丟掉
- 控制在250字內，用數字或換行區隔步驟
- 不回答配方比例、成本、毛利

員工問題：{user_message}"""

    elif msg_type == "qa":
        prompt = f"""你是三入好棧的資深店長，有十年封口機與飲料店現場經驗。

員工正在詢問設備、客訴或現場SOP問題，請用繁體中文直接回答。

回答風格：
- 像LINE訊息，口氣直接像資深店長
- 先講「現在馬上做什麼」，再講原因
- 給具體動作步驟，照順序排列（最常見原因優先）
- 不要只說「重開機」或「聯絡廠商」，要給實際排除步驟
- 如果有高峰期應急替代方案，也要補充說明
- 控制在300字內，用數字或換行區隔步驟
- 禁止建議拆解馬達、電路板、高壓零件等電氣核心
- 不回答配方比例、成本、毛利

員工問題：{user_message}"""

    else:
        prompt = f"""你是三入好棧的資深店長。

員工有問題請教，請用繁體中文直接回答。

回答風格：
- 像資深店長教員工，口氣直接
- 給具體建議，不要廢話
- 控制在200字內
- 不回答配方比例、成本、毛利、未公開加盟資訊

員工問題：{user_message}"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "max_output_tokens": 2048,
            }
        )

        reply_text = response.text if response.text else ""

        if len(reply_text.strip()) < 20:
            reply_text = "我目前判斷不完整，請補充機器型號、錯誤畫面或現場狀況。"

    except Exception as e:
        print(f"[ERROR] ask_gemini_text 失敗: {e}")
        reply_text = "目前系統忙碌，請稍後再試或直接描述問題。"

    return reply_text


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")

    if not signature:
        return "Missing signature", 400

    body = request.get_data(as_text=True)
    handler.handle(body, signature)

    return "OK"


@app.route("/setup-richmenu", methods=["GET"])
def setup_richmenu_route():
    admin_key = os.getenv("ADMIN_SETUP_KEY")
    request_key = request.args.get("key", "")
    user_id = request.args.get("user_id", "").strip()

    if not admin_key:
        return "ADMIN_SETUP_KEY is not configured", 403

    if request_key != admin_key:
        return "Forbidden", 403

    try:
        from setup_richmenu import (
            create_rich_menu,
            create_richmenu_image,
            link_rich_menu_to_user,
            require_token,
            set_default_rich_menu,
            upload_rich_menu_image,
        )

        require_token()
        image_path = create_richmenu_image()
        rich_menu_id = create_rich_menu()
        upload_rich_menu_image(rich_menu_id, image_path)
        set_default_rich_menu(rich_menu_id)

        if user_id:
            link_rich_menu_to_user(rich_menu_id, user_id)

        result = f"Rich Menu 建立完成：{rich_menu_id}"
        if user_id:
            result += f"\n已直接套用至：{user_id}"
        return result, 200

    except Exception as e:
        print("[ERROR] setup-richmenu 失敗:", e)
        return f"Rich Menu 建立失敗：{e}", 500


@app.route("/richmenu-preview", methods=["GET"])
def richmenu_preview_route():
    admin_key = os.getenv("ADMIN_SETUP_KEY")
    request_key = request.args.get("key", "")

    if not admin_key or request_key != admin_key:
        return "Forbidden", 403

    image_path = os.path.join(os.path.dirname(__file__), "richmenu.png")
    if not os.path.exists(image_path):
        return "richmenu.png not found on Render", 404

    return send_file(image_path, mimetype="image/png")


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    state = user_states.get(user_id, {})
    if state.get("flow") == "report_cups":
        reply_to_line(
            event,
            "杯數照片辨識會在第五階段開放，目前請直接輸入杯數數字。"
        )
        return

    if state.get("flow") == "report_mileage":
        reply_to_line(event, "里程回報請直接輸入儀表板公里數，不需要上傳照片。")
        return

    message_id = event.message.id
    image_path = None

    try:
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            image_content = line_bot_blob_api.get_message_content(message_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_file.write(image_content)
            image_path = temp_file.name

        image_file = genai.upload_file(image_path)

        prompt = """你是三入好棧的資深店長，有十年封口機與飲料店現場經驗。

請根據圖片判斷現場問題，直接告訴員工怎麼處理。

回答風格：
- 使用繁體中文，像LINE訊息
- 先說「現在馬上做什麼」
- 給具體動作步驟，照順序排列（最常見原因優先）
- 如果是封口機錯誤代碼，說明可能原因與完整逐步排除方式
- 如果有高峰期應急替代方案（例如切換模式繼續出杯），也要補充
- 控制在300字內，用數字或換行區隔步驟
- 疑似食品異常時，先建議暫停販售
- 禁止建議拆解馬達、電路板、高壓零件等電氣核心
- 不回答配方比例、成本、內部資訊"""

        response = model.generate_content(
            [prompt, image_file],
            generation_config={
                "max_output_tokens": 2048,
            }
        )

        reply_text = (
            response.text
            if response.text
            else "圖片判斷不清楚，請補拍錯誤畫面或問題位置。"
        )

    except Exception as e:
        print(f"[ERROR] handle_image_message 失敗: {e}")
        reply_text = "圖片處理發生問題，請重新傳送或描述狀況。"

    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)

    reply_to_line(event, reply_text)


@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    reply_to_line(
        event,
        "我收到影片了，請再補一句問題，例如：機器一直轉、封口機E07、封膜卡住。"
    )


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data

    print("LINE_USER_ID:", user_id)
    print("POSTBACK_DATA:", data)

    user_info = get_user_role(user_id)
    role = user_info["role"]
    status = user_info["status"]

    if status == "blocked" or role == "blocked":
        reply_to_line(event, "此帳號目前無法使用本系統，請洽總部。")
        return

    if data == "action=confirm_shift":
        start_confirm_shift_flow(event, user_id)
        return

    if data == "action=report_event":
        reply_to_line(event, "📋 事件紀錄功能會在後續階段開放，目前第一階段先完成確認檔期。")
        return

    if data == "action=report_cups":
        if not require_shift_confirmed(event, user_id):
            return
        start_cup_report_flow(event, user_id)
        return

    if data == "action=report_mileage":
        if not require_shift_confirmed(event, user_id):
            return
        start_mileage_report_flow(event, user_id)
        return

    locked_actions = {
        "action=report_materials": "📦 餘料回報",
        "action=report_expense": "💰 費用支出"
    }

    if data in locked_actions:
        if not require_shift_confirmed(event, user_id):
            return

        reply_to_line(event, f"{locked_actions[data]}功能會在後續階段開放，目前第一階段先完成確認檔期。")
        return

    reply_to_line(event, "收到按鍵操作，但目前尚未設定對應功能。")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    print("LINE_USER_ID:", user_id)
    print("USER_MESSAGE:", user_message)

    user_info = get_user_role(user_id)
    role = user_info["role"]
    status = user_info["status"]

    print("ROLE:", role)
    print("STATUS:", status)

    if user_message.strip() == "/reload":
        if user_info["role"] == "admin" and user_info["status"] == "active":
            load_all_data()
            reply_to_line(event, "✅ 資料已重新載入完成")
        else:
            reply_to_line(event, "此指令需要 admin 權限，請洽總部。")
        return

    if user_message.strip() == "/myid":
        reply_to_line(event, f"你的 LINE user_id 是：\n{user_id}")
        return

    if status == "blocked" or role == "blocked":
        reply_to_line(event, "此帳號目前無法使用本系統，請洽總部。")
        return

    if handle_active_flow(event, user_id, user_message):
        return

    msg_type = classify_message(user_message)

    print("MSG_TYPE:", msg_type)

    if msg_type == "recipe":
        recipe = find_recipe(user_message)

        if not recipe:
            reply_to_line(
                event,
                "此問題涉及內部資料，目前資料庫沒有明確答案，請洽總部確認。"
            )
            return

        if status != "active" or role not in ["admin", "franchisee", "staff"]:
            reply_to_line(
                event,
                "此帳號目前沒有查詢配方權限，請洽總部確認。"
            )
            return

        product_name = recipe.get("品項", "此品項")
        recipe_lines = []

        for key, value in recipe.items():
            if value and key not in ["品項"]:
                recipe_lines.append(f"{key}：{value}")

        reply_text = (
            f"{product_name}配方如下：\n"
            + "\n".join(recipe_lines)
        )

        reply_to_line(event, reply_text)
        return

    if msg_type == "qa":
        qa_answer = find_qa_answer(user_message, role, status)

        if qa_answer:
            reply_to_line(event, qa_answer)
            return

        reply_text = ask_gemini_text(user_message, msg_type)
        reply_to_line(event, reply_text)
        return

    if msg_type == "food_quality":
        reply_text = ask_gemini_text(user_message, msg_type)
        reply_to_line(event, reply_text)
        return

    reply_text = ask_gemini_text(user_message, "general")
    reply_to_line(event, reply_text)


load_all_data()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
