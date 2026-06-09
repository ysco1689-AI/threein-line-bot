import os
import json
import tempfile
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import google.generativeai as genai
import gspread

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
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

MATERIAL_ALIASES = {
    "仙草甘茶": ["仙甘", "仙草甘"],
    "大井紅茶": ["大紅", "大井紅", "井"],
    "青茶": ["青茶", "青"],
    "麥香紅茶": ["麥香", "麥香紅"],
    "冬瓜茶": ["冬瓜", "冬"],
    "糖液": ["糖液", "糖水"],
    "仙草凍": ["仙草凍", "仙凍"],
    "檸檬汁": ["檸檬", "檸檬汁"],
    "牛奶": ["牛奶", "奶"],
    "奶水": ["奶水"],
    "冰塊": ["冰塊", "冰"],
    "660紙杯": ["紙杯", "杯子", "660"],
    "杯蓋": ["杯蓋", "蓋子"],
    "大吸管": ["大吸管", "粗管"],
    "小吸管": ["小吸管", "細管"],
    "1杯袋": ["1杯袋", "單杯袋"],
    "2杯袋": ["2杯袋"],
    "4杯袋": ["4杯袋"],
    "封口膜": ["封口膜", "封膜"],
    "試飲杯": ["試飲杯", "試飲"],
}

MATERIAL_SETUP_LABELS = {
    "仙草甘茶": "仙甘（包）",
    "大井紅茶": "大紅（小包）",
    "青茶": "青茶（小包）",
    "麥香紅茶": "麥香紅（小包）",
    "冬瓜茶": "冬瓜（小包）",
    "糖液": "糖液（桶）",
    "仙草凍": "仙草凍（罐）",
    "檸檬汁": "檸檬汁（罐）",
    "牛奶": "牛奶（罐）",
    "奶水": "奶水（罐）",
    "660紙杯": "660紙杯（20條／箱）",
    "杯蓋": "杯蓋（個）",
    "大吸管": "大吸管（包）",
    "小吸管": "小吸管（包）",
    "1杯袋": "1杯袋（包）",
    "2杯袋": "2杯袋（包）",
    "4杯袋": "4杯袋（包）",
    "封口膜": "封口膜（捲）",
    "試飲杯": "試飲杯（40條／箱）",
    "冰塊": "冰塊（包）",
}

EVENT_TYPES = [
    "🥤 杯數問題",
    "📍 位置問題",
    "🏪 攤位問題",
    "👤 人員問題",
    "📦 原料問題",
    "⚡ 設備問題",
    "📋 其他狀況",
]

EVENT_HEADERS = [
    "日期",
    "LINE_ID",
    "姓名",
    "檔期名稱",
    "攤位編號",
    "事件類型",
    "事件說明",
    "照片連結",
    "處理狀態",
    "主管備註",
    "建立時間",
]

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_DRIVE_CLIENT_ID = os.getenv("GOOGLE_DRIVE_CLIENT_ID")
GOOGLE_DRIVE_CLIENT_SECRET = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET")
GOOGLE_DRIVE_REFRESH_TOKEN = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHIFT_SPREADSHEET_ID = os.getenv("SHIFT_SPREADSHEET_ID", SPREADSHEET_ID)
CUP_PHOTO_FOLDER_ID = os.getenv("CUP_PHOTO_FOLDER_ID")
EXPENSE_PHOTO_FOLDER_ID = os.getenv("EXPENSE_PHOTO_FOLDER_ID")
EVENT_PHOTO_FOLDER_ID = os.getenv("EVENT_PHOTO_FOLDER_ID")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


def get_google_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = ServiceAccountCredentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )
    return gspread.authorize(credentials)


def get_drive_credentials():
    missing = [
        name for name, value in [
            ("GOOGLE_DRIVE_CLIENT_ID", GOOGLE_DRIVE_CLIENT_ID),
            ("GOOGLE_DRIVE_CLIENT_SECRET", GOOGLE_DRIVE_CLIENT_SECRET),
            ("GOOGLE_DRIVE_REFRESH_TOKEN", GOOGLE_DRIVE_REFRESH_TOKEN)
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Google Drive OAuth 尚未設定：" + ", ".join(missing)
        )

    return UserCredentials(
        token=None,
        refresh_token=GOOGLE_DRIVE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_DRIVE_CLIENT_ID,
        client_secret=GOOGLE_DRIVE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive"]
    )


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


def safe_filename_part(value, fallback="unknown"):
    text = str(value or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:50] or fallback


def upload_photo_to_drive(
    image_path,
    folder_id,
    category,
    user_name,
    message_id
):
    if not folder_id:
        raise RuntimeError(f"{category} 的 Google Drive 資料夾 ID 尚未設定")

    extension = Path(image_path).suffix.lower() or ".jpg"
    filename = (
        f"{datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')}_"
        f"{safe_filename_part(user_name)}_"
        f"{safe_filename_part(category)}_"
        f"{safe_filename_part(message_id)}{extension}"
    )
    credentials = get_drive_credentials()
    drive_service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )
    media = MediaFileUpload(
        image_path,
        mimetype="image/jpeg",
        resumable=False
    )
    uploaded = drive_service.files().create(
        body={
            "name": filename,
            "parents": [folder_id]
        },
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True
    ).execute()
    file_id = uploaded["id"]
    return {
        "id": file_id,
        "name": uploaded.get("name", filename),
        "url": uploaded.get(
            "webViewLink",
            f"https://drive.google.com/file/d/{file_id}/view"
        )
    }


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


def date_quick_reply(dates, action_prefix="杯數日期"):
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(
                label=value.strftime("%m/%d"),
                text=f"{action_prefix} {value.strftime('%Y/%m/%d')}"
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


def input_method_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="📷 拍照辨識", text="📷 拍照辨識")
        ),
        QuickReplyItem(
            action=MessageAction(label="✏️ 手動輸入", text="✏️ 手動輸入")
        )
    ])


def ai_result_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="✅ 正確", text="✅ AI辨識正確")
        ),
        QuickReplyItem(
            action=MessageAction(label="✏️ 修改", text="✏️ 修改AI結果")
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


def write_new_cup_record(
    user_id,
    shift,
    report_date,
    cups,
    input_method="手動",
    photo_message_id="",
    photo_drive_url=""
):
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
        "",
        input_method,
        photo_message_id,
        photo_drive_url
    ])


def update_cup_record(
    existing_record,
    cups,
    input_method="手動",
    photo_message_id="",
    photo_drive_url=""
):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("杯數回報")
    row_number = existing_record["_row_number"]
    original_cups = str(existing_record.get("杯數", "")).strip()
    sheet.update(
        range_name=f"F{row_number}:M{row_number}",
        values=[[
            cups,
            existing_record.get("填寫時間", "") or now_time_text(),
            "是",
            original_cups,
            now_time_text(),
            input_method,
            photo_message_id,
            photo_drive_url
        ]]
    )


def finish_cup_flow(user_id):
    state = user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("cup_report", None)
    user_states[user_id] = state


def submit_cup_value(event, user_id, state, cups):
    shift = state.get("data", {})
    cup_report = state.setdefault("cup_report", {})
    report_date = cup_report.get("date")
    photo_message_id = cup_report.get("photo_message_id", "")
    photo_drive_url = cup_report.get("photo_drive_url", "")
    input_method = "照片辨識" if photo_message_id else "手動"
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
            write_new_cup_record(
                user_id,
                shift,
                report_date,
                cups,
                input_method,
                photo_message_id,
                photo_drive_url
            )
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
        state["step"] = "waiting_cup_input_method"
        user_states[user_id] = state
        reply_to_line(
            event,
            f"請選擇 {selected_date} 的杯數填寫方式：",
            quick_reply=input_method_quick_reply()
        )
        return True

    if step == "waiting_cup_input_method":
        if message in ["📷 拍照辨識", "拍照辨識", "拍照"]:
            state["step"] = "waiting_cup_photo"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請上傳收銀機畫面或手寫杯數紀錄照片。\n"
                "請讓杯數數字清楚、完整入鏡。"
            )
            return True
        if message in ["✏️ 手動輸入", "手動輸入", "手動"]:
            state["step"] = "waiting_cup_count"
            user_states[user_id] = state
            reply_to_line(event, "請輸入杯數（大於 0 的正整數）。")
            return True
        reply_to_line(
            event,
            "請選擇拍照辨識或手動輸入。",
            quick_reply=input_method_quick_reply()
        )
        return True

    if step == "waiting_cup_count":
        if not re.fullmatch(r"[1-9]\d*", message):
            reply_to_line(event, "杯數必須是大於 0 的整數，請重新輸入。")
            return True

        return submit_cup_value(event, user_id, state, int(message))

    if step == "waiting_cup_ai_confirm":
        if message in ["✅ AI辨識正確", "AI辨識正確", "正確"]:
            cups = cup_report.get("ai_cups")
            return submit_cup_value(event, user_id, state, cups)
        if message in ["✏️ 修改AI結果", "修改AI結果", "修改"]:
            state["step"] = "waiting_cup_count"
            user_states[user_id] = state
            reply_to_line(event, "請輸入正確杯數（大於 0 的正整數）。")
            return True
        reply_to_line(
            event,
            "請確認 AI 辨識結果是否正確。",
            quick_reply=ai_result_quick_reply()
        )
        return True

    if step == "waiting_cup_update_confirm":
        if message in ["✅ 確認修改", "確認修改"]:
            existing = cup_report.get("existing_record")
            cups = cup_report.get("proposed_cups")
            report_date = cup_report.get("date")
            try:
                update_cup_record(
                    existing,
                    cups,
                    "照片辨識" if cup_report.get("photo_message_id") else "手動",
                    cup_report.get("photo_message_id", ""),
                    cup_report.get("photo_drive_url", "")
                )
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


def payment_method_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="A 零用金", text="A 零用金支付")
        ),
        QuickReplyItem(
            action=MessageAction(label="B 私人支付", text="B 私人支付")
        ),
        QuickReplyItem(
            action=MessageAction(label="C 其他", text="C 其他")
        )
    ])


def expense_duplicate_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(
                label="✅ 確認送出",
                text="✅ 不是重複，確認送出"
            )
        ),
        QuickReplyItem(
            action=MessageAction(label="❌ 取消", text="❌ 取消支出")
        )
    ])


def expense_submit_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="✅ 確認送出", text="✅ 確認送出")
        ),
        QuickReplyItem(
            action=MessageAction(label="❌ 取消", text="❌ 取消支出")
        )
    ])


def expense_continue_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="➕ 新增一筆", text="➕ 新增一筆支出")
        ),
        QuickReplyItem(
            action=MessageAction(label="✅ 完成", text="✅ 支出完成")
        )
    ])


def start_expense_report_flow(event, user_id):
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
        "flow": "report_expense",
        "step": "waiting_expense_date",
        "data": shift,
        "expense_report": {}
    })
    user_states[user_id] = state

    date_text = " / ".join(value.strftime("%m/%d") for value in dates)
    reply_to_line(
        event,
        f"💰 請選擇支出日期：\n{date_text}",
        quick_reply=date_quick_reply(dates, "支出日期")
    )


def reset_expense_entry(user_id, keep_date=True):
    state = user_states.get(user_id, {})
    expense_report = state.get("expense_report", {})
    report_date = expense_report.get("date") if keep_date else None
    state["expense_report"] = {}
    if report_date:
        state["expense_report"]["date"] = report_date
    state["step"] = "waiting_expense_input_method" if report_date else "waiting_expense_date"
    user_states[user_id] = state


def finish_expense_flow(user_id):
    state = user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("expense_report", None)
    user_states[user_id] = state


def parse_expense_amount(value):
    text = str(value).strip()
    text = re.sub(r"[,\s元$NTntNTD]", "", text)
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return None
    amount = float(text)
    if amount <= 0:
        return None
    return amount


def format_expense_amount(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def find_duplicate_expenses(user_id, report_date, amount):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("費用支出")
    rows = get_records_by_header(sheet, "LINE_ID")
    matches = []
    for row in rows:
        row_amount = parse_expense_amount(row.get("支出費用", ""))
        if (
            str(row.get("LINE_ID", "")).strip() == user_id
            and normalize_date_text(row.get("日期", "")) == report_date
            and row_amount is not None
            and abs(row_amount - amount) < 0.001
        ):
            matches.append(row)
    return matches


def write_expense_record(user_id, shift, expense_report):
    amount = expense_report["amount"]
    has_receipt = expense_report.get("has_receipt") is True
    review_status = "待審核" if not has_receipt and amount > 2000 else "免審核"
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("費用支出")
    sheet.append_row([
        expense_report.get("date", ""),
        user_id,
        shift.get("name", ""),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        expense_report.get("description", ""),
        format_expense_amount(amount),
        expense_report.get("payment_method", ""),
        expense_report.get("payment_note", ""),
        "有" if has_receipt else "無",
        "" if has_receipt else expense_report.get("no_receipt_reason", ""),
        expense_report.get("photo_drive_url")
        or expense_report.get("photo_message_id", ""),
        "已確認非重複" if expense_report.get("duplicate_confirmed") else "未發現重複",
        review_status,
        now_time_text()
    ])
    return review_status


def show_expense_summary(event, expense_report):
    payment_text = expense_report.get("payment_method", "")
    if expense_report.get("payment_note"):
        payment_text += f"（{expense_report['payment_note']}）"
    lines = [
        "請確認本筆支出：",
        f"日期：{expense_report.get('date', '')}",
        f"說明：{expense_report.get('description', '')}",
        f"金額：NT${format_expense_amount(expense_report.get('amount', 0))}",
        f"付款方式：{payment_text}",
        f"憑證：{'有' if expense_report.get('has_receipt') else '無'}"
    ]
    if not expense_report.get("has_receipt"):
        lines.append(f"無憑證原因：{expense_report.get('no_receipt_reason', '')}")
    lines.extend(["", "請確認是否送出。"])
    reply_to_line(
        event,
        "\n".join(lines),
        quick_reply=expense_submit_quick_reply()
    )


def save_expense_and_continue(event, user_id, state):
    shift = state.get("data", {})
    expense_report = state.get("expense_report", {})
    try:
        review_status = write_expense_record(user_id, shift, expense_report)
    except Exception as e:
        print("[ERROR] 寫入費用支出失敗:", e)
        reply_to_line(event, "寫入費用資料時發生問題，請稍後再試。")
        return True

    state["step"] = "waiting_expense_continue"
    user_states[user_id] = state
    review_note = ""
    if review_status == "待審核":
        review_note = "\n⚠️ 無憑證支出超過 NT$2,000，已標記為待審核。"
    reply_to_line(
        event,
        "💰 支出已記錄完成。"
        f"{review_note}\n\n是否還有其他支出？",
        quick_reply=expense_continue_quick_reply()
    )
    return True


def handle_expense_report_text(event, user_id, user_message):
    state = user_states.get(user_id, {})
    step = state.get("step")
    shift = state.get("data", {})
    expense_report = state.setdefault("expense_report", {})
    message = user_message.strip()

    if step == "waiting_expense_date":
        match = re.fullmatch(r"支出日期\s+(\d{4}/\d{2}/\d{2})", message)
        selected_date = match.group(1) if match else normalize_date_text(message)
        allowed_dates = {
            value.strftime("%Y/%m/%d")
            for value in available_cup_dates(shift)
        }
        if selected_date not in allowed_dates:
            reply_to_line(
                event,
                "請使用下方按鈕選擇支出日期。",
                quick_reply=date_quick_reply(
                    available_cup_dates(shift),
                    "支出日期"
                )
            )
            return True

        expense_report["date"] = selected_date
        state["step"] = "waiting_expense_input_method"
        user_states[user_id] = state
        reply_to_line(
            event,
            "請選擇支出填寫方式：",
            quick_reply=input_method_quick_reply()
        )
        return True

    if step == "waiting_expense_input_method":
        if message in ["📷 拍照辨識", "拍照辨識", "拍照"]:
            expense_report["has_receipt"] = True
            state["step"] = "waiting_expense_photo"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請上傳收據或憑證照片。\n請讓店名、品項與總金額清楚入鏡。"
            )
            return True
        if message in ["✏️ 手動輸入", "手動輸入", "手動"]:
            expense_report["has_receipt"] = False
            state["step"] = "waiting_expense_description"
            user_states[user_id] = state
            reply_to_line(event, "請輸入支出說明，例如：冰塊採購。")
            return True
        reply_to_line(
            event,
            "請選擇拍照辨識或手動輸入。",
            quick_reply=input_method_quick_reply()
        )
        return True

    if step == "waiting_expense_description":
        if len(message) < 2:
            reply_to_line(event, "支出說明至少需要 2 個字，請重新輸入。")
            return True
        expense_report["description"] = message[:100]
        state["step"] = "waiting_expense_amount"
        user_states[user_id] = state
        reply_to_line(event, "請輸入支出金額，例如：250。")
        return True

    if step == "waiting_expense_amount":
        amount = parse_expense_amount(message)
        if amount is None:
            reply_to_line(event, "支出金額必須是大於 0 的數字，請重新輸入。")
            return True
        expense_report["amount"] = amount
        if expense_report.get("has_receipt"):
            state["step"] = "waiting_payment_method"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇付款方式：",
                quick_reply=payment_method_quick_reply()
            )
        else:
            state["step"] = "waiting_no_receipt_reason"
            user_states[user_id] = state
            reply_to_line(event, "請輸入沒有收據或憑證的原因。")
        return True

    if step == "waiting_expense_ai_confirm":
        if message in ["✅ AI辨識正確", "AI辨識正確", "正確"]:
            state["step"] = "waiting_payment_method"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇付款方式：",
                quick_reply=payment_method_quick_reply()
            )
            return True
        if message in ["✏️ 修改AI結果", "修改AI結果", "修改"]:
            state["step"] = "waiting_expense_description"
            user_states[user_id] = state
            reply_to_line(event, "請輸入正確的支出說明。")
            return True
        reply_to_line(
            event,
            "請確認 AI 辨識結果是否正確。",
            quick_reply=ai_result_quick_reply()
        )
        return True

    if step == "waiting_no_receipt_reason":
        if len(message) < 2:
            reply_to_line(event, "請簡短說明無憑證原因，至少 2 個字。")
            return True
        expense_report["no_receipt_reason"] = message[:100]
        state["step"] = "waiting_payment_method"
        user_states[user_id] = state
        reply_to_line(
            event,
            "請選擇付款方式：",
            quick_reply=payment_method_quick_reply()
        )
        return True

    if step == "waiting_payment_method":
        payment_map = {
            "A 零用金支付": "A 零用金支付",
            "A 零用金": "A 零用金支付",
            "B 私人支付": "B 私人支付",
            "C 其他": "C 其他"
        }
        payment_method = payment_map.get(message)
        if not payment_method:
            reply_to_line(
                event,
                "請使用下方按鈕選擇付款方式。",
                quick_reply=payment_method_quick_reply()
            )
            return True
        expense_report["payment_method"] = payment_method
        if payment_method == "C 其他":
            state["step"] = "waiting_payment_note"
            user_states[user_id] = state
            reply_to_line(event, "請說明其他付款方式。")
            return True
        expense_report["payment_note"] = ""
        state["step"] = "checking_expense_duplicate"
        user_states[user_id] = state

    if step == "waiting_payment_note":
        if len(message) < 2:
            reply_to_line(event, "請說明其他付款方式，至少 2 個字。")
            return True
        expense_report["payment_note"] = message[:100]
        state["step"] = "checking_expense_duplicate"
        user_states[user_id] = state

    if state.get("step") == "checking_expense_duplicate":
        try:
            duplicates = find_duplicate_expenses(
                user_id,
                expense_report.get("date", ""),
                expense_report.get("amount", 0)
            )
        except Exception as e:
            print("[ERROR] 查詢重複費用失敗:", e)
            reply_to_line(event, "查詢既有支出時發生問題，請稍後再試。")
            return True

        if duplicates:
            existing = duplicates[-1]
            expense_report["duplicate_confirmed"] = False
            state["step"] = "waiting_expense_duplicate_confirm"
            user_states[user_id] = state
            reply_to_line(
                event,
                "⚠️ 同一天已有一筆相同金額的支出：\n"
                f"NT${format_expense_amount(expense_report['amount'])} / "
                f"{existing.get('支出說明', '')}\n"
                "是否確定不是重複輸入？",
                quick_reply=expense_duplicate_quick_reply()
            )
            return True

        expense_report["duplicate_confirmed"] = False
        state["step"] = "waiting_expense_submit_confirm"
        user_states[user_id] = state
        show_expense_summary(event, expense_report)
        return True

    if step == "waiting_expense_duplicate_confirm":
        if message in ["✅ 不是重複，確認送出", "不是重複", "確認送出"]:
            expense_report["duplicate_confirmed"] = True
            return save_expense_and_continue(event, user_id, state)
        if message in ["❌ 取消支出", "取消支出", "取消"]:
            finish_expense_flow(user_id)
            reply_to_line(event, "已取消本筆支出，資料未寫入。")
            return True
        reply_to_line(
            event,
            "請選擇「✅ 確認送出」或「❌ 取消」。",
            quick_reply=expense_duplicate_quick_reply()
        )
        return True

    if step == "waiting_expense_submit_confirm":
        if message in ["確認送出", "✅ 確認送出", "確認"]:
            return save_expense_and_continue(event, user_id, state)
        if message in ["取消", "❌ 取消支出", "取消支出"]:
            finish_expense_flow(user_id)
            reply_to_line(event, "已取消本筆支出，資料未寫入。")
            return True
        show_expense_summary(event, expense_report)
        return True

    if step == "waiting_expense_continue":
        if message in ["➕ 新增一筆支出", "新增一筆支出", "新增一筆"]:
            reset_expense_entry(user_id, keep_date=True)
            reply_to_line(
                event,
                "請選擇下一筆支出的填寫方式：",
                quick_reply=input_method_quick_reply()
            )
            return True
        if message in ["✅ 支出完成", "支出完成", "完成"]:
            finish_expense_flow(user_id)
            reply_to_line(event, "✅ 今日費用支出回報已完成。")
            return True
        reply_to_line(
            event,
            "請選擇「➕ 新增一筆」或「✅ 完成」。",
            quick_reply=expense_continue_quick_reply()
        )
        return True

    return False


def material_confirm_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="✅ 確認", text="✅ 確認餘料")
        ),
        QuickReplyItem(
            action=MessageAction(label="❌ 取消", text="❌ 取消餘料")
        )
    ])


def material_continue_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="➖ 支出數量", text="支出數量")
        ),
        QuickReplyItem(
            action=MessageAction(label="➕ 入庫數量", text="入庫數量")
        ),
        QuickReplyItem(
            action=MessageAction(label="📦 餘量查詢", text="餘量查詢")
        ),
        QuickReplyItem(
            action=MessageAction(label="✅ 完成", text="✅ 餘料完成")
        )
    ])


def material_setup_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="✅ 帶出完成", text="✅ 帶出完成")
        ),
        QuickReplyItem(
            action=MessageAction(label="📦 查看設定", text="查看帶出設定")
        )
    ])


def event_action_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="📝 新增事件", text="📝 新增事件")
        ),
        QuickReplyItem(
            action=MessageAction(label="🔍 查詢歷史", text="🔍 查詢歷史")
        )
    ])


def event_type_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label=value, text=value))
        for value in EVENT_TYPES
    ])


def event_photo_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label="📷 上傳照片", text="📷 上傳照片")
        ),
        QuickReplyItem(
            action=MessageAction(label="⏭️ 略過", text="⏭️ 略過照片")
        )
    ])


def event_filter_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(
            action=MessageAction(label=value, text=f"事件篩選 {value}")
        )
        for value in ["全部", "待處理", "處理中", "已完成"]
    ])


def parse_material_quantity(value):
    text = str(value or "").strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        return None
    quantity = int(text)
    return quantity if quantity >= 0 else None


def parse_material_transaction_quantity(value):
    text = str(value or "").strip().replace(",", "")
    if not re.fullmatch(r"-?\d+", text):
        return None
    return int(text)


def load_material_settings():
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("原料設定主表")
    rows = get_records_by_header(sheet, "品項名稱")
    settings = []
    for row in rows:
        name = str(row.get("品項名稱", "")).strip()
        if not name:
            continue
        aliases = [name]
        aliases.extend(MATERIAL_ALIASES.get(name, []))
        aliases.extend(
            item.strip()
            for item in re.split(r"[,，、]", str(row.get("簡稱", "")))
            if item.strip()
        )
        settings.append({
            "name": name,
            "aliases": list(dict.fromkeys(aliases)),
            "unit": str(row.get("單位", "")).strip() or "個"
        })
    return settings


def find_material_setting(message, settings):
    compact = re.sub(r"\s+", "", str(message or ""))
    candidates = []
    for setting in settings:
        for alias in setting["aliases"]:
            alias_compact = re.sub(r"\s+", "", alias)
            if alias_compact and alias_compact in compact:
                candidates.append((len(alias_compact), setting))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def extract_material_quantity(message, setting):
    quantity_text = re.sub(r"\s+", "", str(message or ""))
    material_names = [setting["name"], *setting["aliases"]]
    for material_name in sorted(
        set(material_names),
        key=len,
        reverse=True
    ):
        compact_name = re.sub(r"\s+", "", material_name)
        if compact_name and compact_name in quantity_text:
            quantity_text = quantity_text.replace(compact_name, "", 1)
            break
    numbers = re.findall(r"\d+", quantity_text)
    return int(numbers[-1]) if numbers else None


def build_material_initial_template(settings):
    lines = [
        "請複製以下清單，在冒號後填入數字，再整段貼回。",
        "沒有帶出的品項保持空白即可：",
        ""
    ]
    for setting in settings:
        label = MATERIAL_SETUP_LABELS.get(
            setting["name"],
            f"{setting['aliases'][-1]}（{setting['unit']}）"
        )
        lines.append(f"{label}：")
    return "\n".join(lines)


def parse_material_initial_batch(message, settings):
    entries = {}
    errors = []
    for raw_line in str(message or "").splitlines():
        line = raw_line.strip()
        if not line or ("：" not in line and ":" not in line):
            continue
        parts = re.split(r"[：:]", line, maxsplit=1)
        label = parts[0].strip()
        value_text = parts[1].strip()
        if not value_text:
            continue
        quantity_match = re.search(r"\d[\d,]*", value_text)
        if not quantity_match:
            errors.append(f"{label}：請填數字")
            continue
        setting = find_material_setting(label, settings)
        if not setting:
            errors.append(f"{label}：找不到品項")
            continue
        quantity = parse_material_quantity(quantity_match.group(0))
        if quantity is None:
            errors.append(f"{label}：數量格式錯誤")
            continue
        entries[setting["name"]] = (setting, quantity)
    return list(entries.values()), errors


def parse_material_transaction_batch(message, settings):
    entries = []
    errors = []
    for raw_line in str(message or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        setting = find_material_setting(line, settings)
        if not setting:
            errors.append(f"{line}：找不到品項")
            continue
        quantity = extract_material_quantity(line, setting)
        if quantity is None:
            errors.append(f"{line}：請填數字")
            continue
        entries.append((setting, quantity))
    return entries, errors


def get_shift_material_initial_records():
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("檔期原料帶出")
    values = sheet.get_all_values()
    header_index = None
    headers = []
    for index, row in enumerate(values):
        row_headers = [str(cell).strip() for cell in row]
        if "檔期名稱" in row_headers and "帶出量" in row_headers:
            header_index = index
            headers = row_headers
            break
    if header_index is None:
        return []

    records = []
    for row_index in range(header_index + 1, len(values)):
        row = values[row_index]
        if not any(str(cell).strip() for cell in row):
            continue
        record = {
            header: row[col_index] if col_index < len(row) else ""
            for col_index, header in enumerate(headers)
            if header
        }
        record["_row_number"] = row_index + 1
        records.append(record)
    return records


def get_shift_material_initials(shift):
    result = {}
    for row in get_shift_material_initial_records():
        if (
            str(row.get("檔期名稱", "")).strip()
            == str(shift.get("shift_name", "")).strip()
            and str(row.get("攤位編號", "")).strip()
            == str(shift.get("booth", "")).strip()
            and normalize_date_text(row.get("開始日期", ""))
            == normalize_date_text(shift.get("start_date", ""))
            and normalize_date_text(row.get("結束日期", ""))
            == normalize_date_text(shift.get("end_date", ""))
        ):
            quantity = parse_material_quantity(row.get("帶出量", ""))
            if quantity is not None:
                result[str(row.get("品項名稱", "")).strip()] = quantity
    return result


def save_shift_material_initial(user_id, shift, setting, quantity):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("檔期原料帶出")
    existing = None
    for row in get_shift_material_initial_records():
        if (
            str(row.get("檔期名稱", "")).strip()
            == str(shift.get("shift_name", "")).strip()
            and str(row.get("攤位編號", "")).strip()
            == str(shift.get("booth", "")).strip()
            and normalize_date_text(row.get("開始日期", ""))
            == normalize_date_text(shift.get("start_date", ""))
            and normalize_date_text(row.get("結束日期", ""))
            == normalize_date_text(shift.get("end_date", ""))
            and str(row.get("品項名稱", "")).strip() == setting["name"]
        ):
            existing = row
            break

    if existing:
        row_number = existing["_row_number"]
        sheet.update(
            range_name=f"F{row_number}:J{row_number}",
            values=[[
                quantity,
                setting["unit"],
                shift.get("name", ""),
                user_id,
                f"{today_text()} {now_time_text()}"
            ]]
        )
    else:
        sheet.append_row([
            shift.get("shift_name", ""),
            shift.get("booth", ""),
            shift.get("start_date", ""),
            shift.get("end_date", ""),
            setting["name"],
            quantity,
            setting["unit"],
            shift.get("name", ""),
            user_id,
            f"{today_text()} {now_time_text()}"
        ])


def save_shift_material_initial_batch(user_id, shift, entries):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("檔期原料帶出")
    existing_records = get_shift_material_initial_records()
    existing_by_name = {}
    for row in existing_records:
        if (
            str(row.get("檔期名稱", "")).strip()
            == str(shift.get("shift_name", "")).strip()
            and str(row.get("攤位編號", "")).strip()
            == str(shift.get("booth", "")).strip()
            and normalize_date_text(row.get("開始日期", ""))
            == normalize_date_text(shift.get("start_date", ""))
            and normalize_date_text(row.get("結束日期", ""))
            == normalize_date_text(shift.get("end_date", ""))
        ):
            existing_by_name[str(row.get("品項名稱", "")).strip()] = row

    update_requests = []
    append_values = []
    updated_at = f"{today_text()} {now_time_text()}"
    for setting, quantity in entries:
        existing = existing_by_name.get(setting["name"])
        if existing:
            row_number = existing["_row_number"]
            update_requests.append({
                "range": f"F{row_number}:J{row_number}",
                "values": [[
                    quantity,
                    setting["unit"],
                    shift.get("name", ""),
                    user_id,
                    updated_at
                ]]
            })
        else:
            append_values.append([
                shift.get("shift_name", ""),
                shift.get("booth", ""),
                shift.get("start_date", ""),
                shift.get("end_date", ""),
                setting["name"],
                quantity,
                setting["unit"],
                shift.get("name", ""),
                user_id,
                updated_at
            ])

    if update_requests:
        sheet.batch_update(update_requests)
    if append_values:
        sheet.append_rows(append_values)


def get_material_records():
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    values = sheet.get_all_values()
    header_index = None
    headers = []
    for index, row in enumerate(values):
        row_headers = [str(cell).strip() for cell in row]
        if "LINE_ID" in row_headers and "品項名稱" in row_headers:
            header_index = index
            headers = row_headers
            break
    if header_index is None:
        return []

    records = []
    for row_index in range(header_index + 1, len(values)):
        row = values[row_index]
        if not any(str(cell).strip() for cell in row):
            continue
        record = {
            header: row[col_index] if col_index < len(row) else ""
            for col_index, header in enumerate(headers)
            if header
        }
        record["_row_number"] = row_index + 1
        records.append(record)
    return records


def material_records_for_shift(
    records,
    shift_name,
    booth,
    start_date,
    end_date,
    material_name
):
    return [
        row for row in records
        if (
            str(row.get("檔期名稱", "")).strip() == shift_name
            and str(row.get("攤位編號", "")).strip() == booth
            and normalize_date_text(row.get("開始日期", ""))
            == normalize_date_text(start_date)
            and normalize_date_text(row.get("結束日期", ""))
            == normalize_date_text(end_date)
            and str(row.get("品項名稱", "")).strip() == material_name
        )
    ]


def calculate_material_used(
    records,
    shift_name,
    booth,
    start_date,
    end_date,
    material_name
):
    total = 0
    for row in material_records_for_shift(
        records,
        shift_name,
        booth,
        start_date,
        end_date,
        material_name
    ):
        quantity = parse_material_transaction_quantity(
            row.get("本次使用量", "")
        )
        if quantity is not None:
            total += quantity
    return total


def material_report_date(shift):
    today = parse_date(today_text())
    end_date = parse_date(shift.get("end_date", ""))
    if today and end_date and today > end_date:
        return end_date.strftime("%Y/%m/%d")
    return today_text()


def is_material_final_day(shift, report_date):
    return (
        normalize_date_text(report_date)
        == normalize_date_text(shift.get("end_date", ""))
    )


def write_material_record(user_id, shift, setting, quantity, remaining):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    report_date = material_report_date(shift)
    sheet.append_row([
        report_date,
        user_id,
        shift.get("name", ""),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        shift.get("start_date", ""),
        shift.get("end_date", ""),
        setting["name"],
        setting["initial"],
        quantity,
        remaining,
        setting["unit"],
        "是" if is_material_final_day(shift, report_date) else "否",
        now_time_text()
    ])


def write_material_records_batch(user_id, shift, entries):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    report_date = material_report_date(shift)
    written_at = now_time_text()
    rows = []
    for setting, quantity, remaining in entries:
        rows.append([
            report_date,
            user_id,
            shift.get("name", ""),
            shift.get("shift_name", ""),
            shift.get("booth", ""),
            shift.get("start_date", ""),
            shift.get("end_date", ""),
            setting["name"],
            setting["initial"],
            quantity,
            remaining,
            setting["unit"],
            "是" if is_material_final_day(shift, report_date) else "否",
            written_at
        ])
    if rows:
        sheet.append_rows(rows)


def recompute_material_balances(shift, setting):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    records = material_records_for_shift(
        get_material_records(),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        shift.get("start_date", ""),
        shift.get("end_date", ""),
        setting["name"]
    )
    remaining = setting["initial"]
    for row in records:
        quantity = parse_material_transaction_quantity(
            row.get("本次使用量", "")
        ) or 0
        remaining -= quantity
        sheet.update_acell(f"K{row['_row_number']}", remaining)
    return remaining


def recompute_material_balances_batch(shift, settings):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    records = get_material_records()
    update_requests = []
    remaining_by_name = {}
    for setting in settings:
        remaining = setting["initial"]
        matching_records = material_records_for_shift(
            records,
            shift.get("shift_name", ""),
            shift.get("booth", ""),
            shift.get("start_date", ""),
            shift.get("end_date", ""),
            setting["name"]
        )
        for row in matching_records:
            quantity = parse_material_transaction_quantity(
                row.get("本次使用量", "")
            ) or 0
            remaining -= quantity
            update_requests.append({
                "range": f"K{row['_row_number']}",
                "values": [[remaining]]
            })
        remaining_by_name[setting["name"]] = remaining

    if update_requests:
        sheet.batch_update(update_requests)
    return remaining_by_name


def find_latest_material_record(
    user_id,
    shift_name,
    booth,
    start_date,
    end_date,
    material_name
):
    matches = [
        row for row in get_material_records()
        if (
            str(row.get("LINE_ID", "")).strip() == user_id
            and str(row.get("檔期名稱", "")).strip() == shift_name
            and str(row.get("攤位編號", "")).strip() == booth
            and normalize_date_text(row.get("開始日期", ""))
            == normalize_date_text(start_date)
            and normalize_date_text(row.get("結束日期", ""))
            == normalize_date_text(end_date)
            and str(row.get("品項名稱", "")).strip() == material_name
        )
    ]
    return matches[-1] if matches else None


def update_material_record(existing_record, shift, setting, quantity):
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    row_number = existing_record["_row_number"]
    sheet.update_acell(f"J{row_number}", quantity)
    sheet.update_acell(f"N{row_number}", now_time_text())
    return recompute_material_balances(shift, setting)


def show_material_total(event, shift, settings):
    records = get_material_records()
    initials = get_shift_material_initials(shift)
    lines = [f"📦 目前餘量（{material_report_date(shift)[5:]} 更新）"]
    for setting in settings:
        initial = initials.get(setting["name"])
        if initial is None:
            continue
        used = calculate_material_used(
            records,
            shift.get("shift_name", ""),
            shift.get("booth", ""),
            shift.get("start_date", ""),
            shift.get("end_date", ""),
            setting["name"]
        )
        remaining = initial - used
        lines.append(
            f"{setting['name']}：帶出 {initial}，"
            f"剩餘 {remaining} {setting['unit']}"
        )
    if not initials:
        lines.append("\n此檔期尚未設定原料帶出量。")
    reply_to_line(
        event,
        "\n".join(lines),
        quick_reply=material_continue_quick_reply()
    )


def show_material_initials(event, shift, settings):
    initials = get_shift_material_initials(shift)
    lines = ["⚙️ 本檔期原料帶出設定"]
    for setting in settings:
        initial = initials.get(setting["name"])
        if initial is not None:
            lines.append(
                f"{setting['name']}：{initial} {setting['unit']}"
            )
    if not initials:
        lines.append("尚未設定任何品項。")
    lines.append("\n可繼續輸入，例如：仙甘50包")
    reply_to_line(
        event,
        "\n".join(lines),
        quick_reply=material_setup_quick_reply()
    )


def start_material_report_flow(event, user_id):
    shift = get_confirmed_shift(user_id)
    if not shift:
        reply_to_line(event, "找不到目前可回報的檔期，請聯絡主管確認排班。")
        return
    try:
        settings = load_material_settings()
    except Exception as e:
        print("[ERROR] 讀取原料設定主表失敗:", e)
        reply_to_line(event, "讀取原料設定時發生問題，請稍後再試。")
        return

    try:
        initials = get_shift_material_initials(shift)
    except Exception as e:
        print("[ERROR] 讀取檔期原料帶出失敗:", e)
        reply_to_line(event, "讀取本檔期原料設定時發生問題，請稍後再試。")
        return

    state = user_states.get(user_id, {})
    state.update({
        "flow": "report_materials",
        "step": (
            "waiting_material_message"
            if initials
            else "waiting_material_initial"
        ),
        "data": shift,
        "material_settings": settings,
        "material_pending": {}
    })
    user_states[user_id] = state
    shift_summary = (
        f"姓名：{shift.get('name', '')}\n"
        f"檔期：{shift.get('shift_name', '')}\n"
        f"攤位：{shift.get('booth', '')}\n"
        f"期間：{shift.get('start_date', '')}～{shift.get('end_date', '')}\n\n"
    )
    if not initials:
        reply_to_line(
            event,
            shift_summary
            + "⚙️ 此檔期尚未設定原料帶出量。\n"
            + build_material_initial_template(settings)
        )
        return

    reply_to_line(
        event,
        shift_summary
        + "📦 帶出量已設定完成，請選擇操作：\n"
        "➖ 支出數量：扣除現場使用量\n"
        "➕ 入庫數量：增加補貨或退回量\n"
        "📦 餘量查詢：查看目前庫存\n"
        "✅ 完成：結束本次操作",
        quick_reply=material_continue_quick_reply()
    )


def finish_material_flow(user_id):
    state = user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("material_settings", None)
    state.pop("material_pending", None)
    user_states[user_id] = state


def handle_material_report_text(event, user_id, user_message):
    state = user_states.get(user_id, {})
    step = state.get("step")
    shift = state.get("data", {})
    try:
        settings = (
            state.get("material_settings")
            or load_material_settings()
        )
    except Exception as e:
        print("[ERROR] 讀取原料設定失敗:", e)
        reply_to_line(event, "讀取原料設定時發生問題，請稍後再試。")
        return True
    message = user_message.strip()
    compact = re.sub(r"\s+", "", message)

    if message in ["✅ 餘料完成", "餘料完成", "完成", "退出"]:
        finish_material_flow(user_id)
        reply_to_line(event, "✅ 餘料回報已完成。")
        return True

    if message in ["設定帶出量", "修改帶出量", "帶出設定"]:
        try:
            initials = get_shift_material_initials(shift)
        except Exception as e:
            print("[ERROR] 檢查檔期原料帶出失敗:", e)
            reply_to_line(event, "讀取帶出設定時發生問題，請稍後再試。")
            return True
        if initials:
            state["step"] = "waiting_material_message"
            user_states[user_id] = state
            reply_to_line(
                event,
                "本檔期帶出量已設定完成，不需要再次設定。\n"
                "請選擇支出、入庫、餘量查詢或完成。",
                quick_reply=material_continue_quick_reply()
            )
            return True
        state["step"] = "waiting_material_initial"
        user_states[user_id] = state
        reply_to_line(
            event,
            "⚙️ 請設定本檔期帶出量。\n"
            + build_material_initial_template(settings)
        )
        return True

    if step == "waiting_material_initial":
        if message in ["✅ 帶出完成", "帶出完成", "設定完成"]:
            try:
                initials = get_shift_material_initials(shift)
            except Exception as e:
                print("[ERROR] 檢查檔期原料帶出失敗:", e)
                reply_to_line(event, "讀取帶出設定時發生問題，請稍後再試。")
                return True
            if not initials:
                reply_to_line(
                    event,
                    "目前尚未設定任何原料，請至少輸入一項帶出量。",
                    quick_reply=material_setup_quick_reply()
                )
                return True
            state["step"] = "waiting_material_message"
            user_states[user_id] = state
            reply_to_line(
                event,
                "✅ 本檔期帶出量設定完成。\n"
                "請選擇支出數量、入庫數量、餘量查詢或完成。",
                quick_reply=material_continue_quick_reply()
            )
            return True

        if message in ["查看帶出設定", "查看設定", "帶出總表"]:
            try:
                show_material_initials(event, shift, settings)
            except Exception as e:
                print("[ERROR] 顯示檔期原料帶出失敗:", e)
                reply_to_line(event, "讀取帶出設定時發生問題，請稍後再試。")
            return True

        batch_entries, batch_errors = parse_material_initial_batch(
            message,
            settings
        )
        if batch_entries:
            saved_lines = []
            try:
                save_shift_material_initial_batch(
                    user_id,
                    shift,
                    batch_entries
                )
                settings_with_initial = []
                for setting, quantity in batch_entries:
                    setting_with_initial = dict(setting)
                    setting_with_initial["initial"] = quantity
                    settings_with_initial.append(setting_with_initial)
                    saved_lines.append(
                        f"{setting['name']} {quantity} {setting['unit']}"
                    )
                recompute_material_balances_batch(
                    shift,
                    settings_with_initial
                )
            except Exception as e:
                print("[ERROR] 批次儲存檔期原料帶出失敗:", e)
                reply_to_line(event, "儲存帶出量時發生問題，請稍後再試。")
                return True

            reply_lines = [
                f"✅ 本檔期帶出量設定完成，共 {len(saved_lines)} 項：",
                *saved_lines
            ]
            if batch_errors:
                reply_lines.extend([
                    "",
                    "以下內容未寫入：",
                    *batch_errors
                ])
            reply_lines.append(
                "\n請選擇支出數量、入庫數量、餘量查詢或完成。"
            )
            state["step"] = "waiting_material_message"
            user_states[user_id] = state
            reply_to_line(
                event,
                "\n".join(reply_lines),
                quick_reply=material_continue_quick_reply()
            )
            return True

        setting = find_material_setting(message, settings)
        if not setting:
            reply_to_line(
                event,
                "找不到此品項，請輸入品項與帶出量，例如：仙甘50包。",
                quick_reply=material_setup_quick_reply()
            )
            return True
        quantity = extract_material_quantity(message, setting)
        if quantity is None:
            reply_to_line(
                event,
                f"請輸入{setting['name']}的帶出數量，例如："
                f"{setting['aliases'][-1]}50{setting['unit']}。"
            )
            return True
        try:
            save_shift_material_initial(
                user_id,
                shift,
                setting,
                quantity
            )
            setting_with_initial = dict(setting)
            setting_with_initial["initial"] = quantity
            recompute_material_balances(shift, setting_with_initial)
        except Exception as e:
            print("[ERROR] 儲存檔期原料帶出失敗:", e)
            reply_to_line(event, "儲存帶出量時發生問題，請稍後再試。")
            return True
        reply_to_line(
            event,
            f"✅ {setting['name']}帶出量已設定為 "
            f"{quantity} {setting['unit']}。\n"
            "請選擇支出數量、入庫數量、餘量查詢或完成。",
            quick_reply=material_continue_quick_reply()
        )
        state["step"] = "waiting_material_message"
        user_states[user_id] = state
        return True

    if step == "waiting_material_overuse_confirm":
        if message in ["❌ 取消餘料", "取消餘料", "取消"]:
            state["step"] = "waiting_material_message"
            state["material_pending"] = {}
            user_states[user_id] = state
            reply_to_line(
                event,
                "已取消，資料未寫入。可繼續輸入下一筆餘料。",
                quick_reply=material_continue_quick_reply()
            )
            return True
        if message not in ["✅ 確認餘料", "確認餘料", "確認"]:
            reply_to_line(
                event,
                "請選擇「✅ 確認」或「❌ 取消」。",
                quick_reply=material_confirm_quick_reply()
            )
            return True

        pending = state.get("material_pending", {})
        setting = pending.get("setting")
        quantity = pending.get("quantity")
        try:
            write_material_record(
                user_id,
                shift,
                setting,
                quantity,
                pending.get("remaining")
            )
            remaining = pending.get("remaining")
            reply_text = (
                f"📦 {setting['name']}已支出 {quantity} "
                f"{setting['unit']}，目前餘量 {remaining} "
                f"{setting['unit']}。"
            )
        except Exception as e:
            print("[ERROR] 確認餘料資料失敗:", e)
            reply_to_line(event, "寫入餘料資料時發生問題，請稍後再試。")
            return True

        state["step"] = "waiting_material_message"
        state["material_pending"] = {}
        user_states[user_id] = state
        reply_to_line(
            event,
            reply_text,
            quick_reply=material_continue_quick_reply()
        )
        return True

    if message in ["➖ 支出數量", "支出數量"]:
        state["step"] = "waiting_material_outbound"
        state["material_pending"] = {}
        user_states[user_id] = state
        reply_to_line(
            event,
            "➖ 請輸入支出的品項與數量，例如：仙甘4包。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    if message in ["➕ 入庫數量", "入庫數量"]:
        state["step"] = "waiting_material_inbound"
        state["material_pending"] = {}
        user_states[user_id] = state
        reply_to_line(
            event,
            "➕ 請輸入入庫的品項與數量，例如：仙甘10包。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    if compact in [
        "餘量查詢",
        "餘料總表",
        "總表",
        "請給我餘料",
        "餘料清單"
    ]:
        try:
            state["step"] = "waiting_material_message"
            state["material_pending"] = {}
            user_states[user_id] = state
            show_material_total(event, shift, settings)
        except Exception as e:
            print("[ERROR] 顯示餘料總表失敗:", e)
            reply_to_line(event, "讀取餘料總表時發生問題，請稍後再試。")
        return True

    if step == "waiting_material_message":
        reply_to_line(
            event,
            "請先選擇支出數量、入庫數量、餘量查詢或完成。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    batch_entries, batch_errors = parse_material_transaction_batch(
        message,
        settings
    )
    if len(batch_entries) > 1:
        try:
            initials = get_shift_material_initials(shift)
            records = get_material_records()
        except Exception as e:
            print("[ERROR] 讀取批次餘料資料失敗:", e)
            reply_to_line(event, "讀取餘料資料時發生問題，請稍後再試。")
            return True

        is_inbound = step == "waiting_material_inbound"
        remaining_by_name = {}
        write_entries = []
        reply_lines = []
        for setting, quantity in batch_entries:
            initial = initials.get(setting["name"])
            if initial is None:
                batch_errors.append(f"{setting['aliases'][-1]}：不在本檔期帶出品項")
                continue
            setting = dict(setting)
            setting["initial"] = initial
            if setting["name"] not in remaining_by_name:
                used = calculate_material_used(
                    records,
                    shift.get("shift_name", ""),
                    shift.get("booth", ""),
                    shift.get("start_date", ""),
                    shift.get("end_date", ""),
                    setting["name"]
                )
                remaining_by_name[setting["name"]] = initial - used

            current_remaining = remaining_by_name[setting["name"]]
            transaction_quantity = -quantity if is_inbound else quantity
            new_remaining = current_remaining - transaction_quantity
            if not is_inbound and quantity > current_remaining:
                batch_errors.append(
                    f"{setting['name']}：目前餘量 {current_remaining}，支出 {quantity} 會超過"
                )
                continue
            remaining_by_name[setting["name"]] = new_remaining
            write_entries.append((setting, transaction_quantity, new_remaining))
            action_text = "入庫" if is_inbound else "支出"
            reply_lines.append(
                f"{setting['name']} {action_text} {quantity} {setting['unit']}，餘量 {new_remaining}"
            )

        if not write_entries:
            reply_to_line(
                event,
                "這批資料沒有成功寫入。\n" + "\n".join(batch_errors),
                quick_reply=material_continue_quick_reply()
            )
            return True

        try:
            write_material_records_batch(user_id, shift, write_entries)
        except Exception as e:
            print("[ERROR] 批次寫入餘料回報失敗:", e)
            reply_to_line(event, "寫入餘料資料時發生問題，請稍後再試。")
            return True

        state["step"] = "waiting_material_message"
        state["material_pending"] = {}
        user_states[user_id] = state
        response_lines = [f"✅ 已完成 {len(write_entries)} 筆：", *reply_lines]
        if batch_errors:
            response_lines.extend(["", "以下未寫入：", *batch_errors])
        reply_to_line(
            event,
            "\n".join(response_lines),
            quick_reply=material_continue_quick_reply()
        )
        return True

    setting = find_material_setting(message, settings)
    if not setting:
        reply_to_line(
            event,
            "找不到此品項，請輸入「餘料總表」查看完整品項列表。",
            quick_reply=material_continue_quick_reply()
        )
        return True
    try:
        initial = get_shift_material_initials(shift).get(setting["name"])
    except Exception as e:
        print("[ERROR] 讀取品項帶出量失敗:", e)
        reply_to_line(event, "讀取本檔期帶出量時發生問題，請稍後再試。")
        return True
    if initial is None:
        reply_to_line(
            event,
            f"{setting['name']}不在本檔期的帶出品項中，請確認品項。",
            quick_reply=material_continue_quick_reply()
        )
        return True
    setting = dict(setting)
    setting["initial"] = initial

    try:
        records = get_material_records()
        total_used = calculate_material_used(
            records,
            shift.get("shift_name", ""),
            shift.get("booth", ""),
            shift.get("start_date", ""),
            shift.get("end_date", ""),
            setting["name"]
        )
    except Exception as e:
        print("[ERROR] 讀取餘料紀錄失敗:", e)
        reply_to_line(event, "讀取餘料資料時發生問題，請稍後再試。")
        return True
    current_remaining = setting["initial"] - total_used

    query_words = ["剩餘", "剩多少", "還有多少", "多少了"]
    if any(word in compact for word in query_words):
        reply_to_line(
            event,
            f"📦 {setting['name']}：系統帶出 {setting['initial']} "
            f"{setting['unit']}，已使用 {total_used} {setting['unit']}，"
            f"目前剩餘 {current_remaining} {setting['unit']}。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    quantity = extract_material_quantity(message, setting)
    if quantity is None:
        reply_to_line(
            event,
            f"請在{setting['name']}後面加上數量，例如："
            f"{setting['aliases'][-1]}4{setting['unit']}。"
        )
        return True

    is_inbound = step == "waiting_material_inbound"
    transaction_quantity = -quantity if is_inbound else quantity
    new_remaining = current_remaining - transaction_quantity
    if not is_inbound and quantity > current_remaining:
        state["step"] = "waiting_material_overuse_confirm"
        state["material_pending"] = {
            "setting": setting,
            "quantity": quantity,
            "remaining": new_remaining
        }
        user_states[user_id] = state
        reply_to_line(
            event,
            f"⚠️ {setting['name']}目前剩餘 {current_remaining} "
            f"{setting['unit']}，使用量 {quantity} {setting['unit']}已超過剩餘。\n"
            "是否仍要確認？",
            quick_reply=material_confirm_quick_reply()
        )
        return True

    try:
        write_material_record(
            user_id,
            shift,
            setting,
            transaction_quantity,
            new_remaining
        )
    except Exception as e:
        print("[ERROR] 寫入餘料回報失敗:", e)
        reply_to_line(event, "寫入餘料資料時發生問題，請稍後再試。")
        return True

    state["step"] = "waiting_material_message"
    state["material_pending"] = {}
    user_states[user_id] = state
    action_text = "已入庫" if is_inbound else "已支出"
    reply_to_line(
        event,
        f"📦 {setting['name']}{action_text} {quantity} {setting['unit']}，"
        f"目前餘量 {new_remaining} {setting['unit']}。",
        quick_reply=material_continue_quick_reply()
    )
    return True


def get_user_name(user_id):
    for user in users_cache:
        if str(user.get("line_user_id", "")).strip() == user_id:
            name = str(user.get("name", "")).strip()
            if name:
                return name
    return "新使用者"


def get_event_sheet():
    spreadsheet = get_shift_spreadsheet()
    try:
        sheet = spreadsheet.worksheet("事件紀錄")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title="事件紀錄",
            rows=1000,
            cols=len(EVENT_HEADERS)
        )
        sheet.append_row(EVENT_HEADERS)
        return sheet

    values = sheet.get_all_values()
    if not values:
        sheet.append_row(EVENT_HEADERS)
    return sheet


def get_event_context(user_id):
    try:
        shift = find_accessible_shift(user_id)
    except Exception as e:
        print("[ERROR] 事件紀錄查詢檔期失敗:", e)
        shift = None
    return {
        "name": (
            shift.get("name", "")
            if shift
            else get_user_name(user_id)
        ),
        "shift_name": shift.get("shift_name", "") if shift else "無",
        "booth": shift.get("booth", "") if shift else "無"
    }


def write_event_record(user_id, event_report):
    context = get_event_context(user_id)
    sheet = get_event_sheet()
    sheet.append_row([
        today_text(),
        user_id,
        context["name"],
        context["shift_name"] or "無",
        context["booth"] or "無",
        event_report.get("event_type", ""),
        event_report.get("description", ""),
        event_report.get("photo_drive_url")
        or event_report.get("photo_message_id", ""),
        "待處理",
        "",
        now_time_text()
    ])


def get_event_records(user_id, status_filter="全部"):
    sheet = get_event_sheet()
    rows = get_records_by_header(sheet, "LINE_ID")
    today = parse_date(today_text())
    first_date = today - timedelta(days=29)
    matches = []
    for row in rows:
        event_date = parse_date(row.get("日期", ""))
        if (
            str(row.get("LINE_ID", "")).strip() != user_id
            or not event_date
            or event_date < first_date
            or event_date > today
        ):
            continue
        status = str(row.get("處理狀態", "")).strip() or "待處理"
        if status_filter != "全部" and status != status_filter:
            continue
        matches.append(row)
    return list(reversed(matches))


def finish_event_flow(user_id):
    state = user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("event_report", None)
    user_states[user_id] = state


def start_event_flow(event, user_id):
    state = user_states.get(user_id, {})
    state.update({
        "flow": "report_event",
        "step": "waiting_event_action",
        "event_report": {}
    })
    user_states[user_id] = state
    reply_to_line(
        event,
        "📋 請選擇事件紀錄操作：",
        quick_reply=event_action_quick_reply()
    )


def submit_event_record(event, user_id, state):
    event_report = state.get("event_report", {})
    try:
        write_event_record(user_id, event_report)
    except Exception as e:
        print("[ERROR] 寫入事件紀錄失敗:", e)
        reply_to_line(event, "寫入事件紀錄時發生問題，請稍後再試。")
        return True

    finish_event_flow(user_id)
    reply_to_line(event, "📋 事件已記錄，主管會盡快處理。")
    return True


def show_event_history(event, user_id, status_filter):
    try:
        records = get_event_records(user_id, status_filter)
    except Exception as e:
        print("[ERROR] 查詢事件紀錄失敗:", e)
        reply_to_line(event, "查詢事件紀錄時發生問題，請稍後再試。")
        return

    finish_event_flow(user_id)
    filter_text = "" if status_filter == "全部" else status_filter
    if not records:
        reply_to_line(event, f"近 30 天無{filter_text}的事件紀錄。")
        return

    lines = [f"📋 近 30 天事件紀錄（{status_filter}）"]
    for row in records[:10]:
        description = str(row.get("事件說明", "")).strip()
        if len(description) > 20:
            description = description[:20] + "..."
        lines.extend([
            "",
            f"{row.get('日期', '')} | {row.get('事件類型', '')}",
            description,
            f"狀態：{row.get('處理狀態', '') or '待處理'}"
        ])
    if len(records) > 10:
        lines.append(f"\n共 {len(records)} 筆，顯示最近 10 筆。")
    reply_to_line(event, "\n".join(lines))


def handle_event_report_text(event, user_id, user_message):
    state = user_states.get(user_id, {})
    step = state.get("step")
    event_report = state.setdefault("event_report", {})
    message = user_message.strip()

    if message in ["取消事件", "取消", "退出"]:
        finish_event_flow(user_id)
        reply_to_line(event, "已取消事件紀錄。")
        return True

    if step == "waiting_event_action":
        if message in ["📝 新增事件", "新增事件"]:
            state["step"] = "waiting_event_type"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇事件類型：",
                quick_reply=event_type_quick_reply()
            )
            return True
        if message in ["🔍 查詢歷史", "查詢歷史"]:
            state["step"] = "waiting_event_filter"
            user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇篩選條件：",
                quick_reply=event_filter_quick_reply()
            )
            return True
        reply_to_line(
            event,
            "請選擇新增事件或查詢歷史。",
            quick_reply=event_action_quick_reply()
        )
        return True

    if step == "waiting_event_type":
        if message not in EVENT_TYPES:
            reply_to_line(
                event,
                "請使用下方按鈕選擇事件類型。",
                quick_reply=event_type_quick_reply()
            )
            return True
        event_report["event_type"] = message
        state["step"] = "waiting_event_description"
        user_states[user_id] = state
        reply_to_line(event, "請描述事件內容。")
        return True

    if step == "waiting_event_description":
        if len(message) < 2:
            reply_to_line(event, "事件說明至少需要 2 個字，請重新輸入。")
            return True
        event_report["description"] = message[:500]
        state["step"] = "waiting_event_photo_choice"
        user_states[user_id] = state
        reply_to_line(
            event,
            "是否需要附上照片？",
            quick_reply=event_photo_quick_reply()
        )
        return True

    if step == "waiting_event_photo_choice":
        if message in ["📷 上傳照片", "上傳照片", "照片"]:
            state["step"] = "waiting_event_photo"
            user_states[user_id] = state
            reply_to_line(event, "請上傳事件照片。")
            return True
        if message in ["⏭️ 略過照片", "略過照片", "略過"]:
            return submit_event_record(event, user_id, state)
        reply_to_line(
            event,
            "請選擇上傳照片或略過。",
            quick_reply=event_photo_quick_reply()
        )
        return True

    if step == "waiting_event_photo":
        reply_to_line(event, "目前正在等待事件照片，請直接上傳圖片。")
        return True

    if step == "waiting_event_filter":
        match = re.fullmatch(r"事件篩選\s+(全部|待處理|處理中|已完成)", message)
        status_filter = match.group(1) if match else message
        if status_filter not in ["全部", "待處理", "處理中", "已完成"]:
            reply_to_line(
                event,
                "請使用下方按鈕選擇篩選條件。",
                quick_reply=event_filter_quick_reply()
            )
            return True
        show_event_history(event, user_id, status_filter)
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

    if state.get("flow") == "report_expense":
        return handle_expense_report_text(event, user_id, user_message)

    if state.get("flow") == "report_materials":
        return handle_material_report_text(event, user_id, user_message)

    if state.get("flow") == "report_event":
        return handle_event_report_text(event, user_id, user_message)

    return False


def handle_material_template_recovery(event, user_id, user_message):
    colon_lines = [
        line for line in str(user_message or "").splitlines()
        if "：" in line or ":" in line
    ]
    if len(colon_lines) < 3:
        return False

    try:
        settings = load_material_settings()
        batch_entries, _ = parse_material_initial_batch(
            user_message,
            settings
        )
    except Exception as e:
        print("[ERROR] 辨識帶出量清單失敗:", e)
        return False

    if not batch_entries:
        return False

    if not check_shift_confirmed(user_id):
        reply_to_line(
            event,
            "這看起來是原料帶出量清單，請先點選「✅ 確認檔期」，"
            "再進入餘料回報重新貼上。"
        )
        return True

    shift = get_confirmed_shift(user_id)
    if not shift:
        reply_to_line(event, "找不到目前檔期，請聯絡主管確認排班。")
        return True

    try:
        initials = get_shift_material_initials(shift)
    except Exception as e:
        print("[ERROR] 檢查檔期帶出量失敗:", e)
        reply_to_line(event, "讀取帶出設定時發生問題，請稍後再試。")
        return True

    if initials:
        state = user_states.get(user_id, {})
        state.update({
            "flow": "report_materials",
            "step": "waiting_material_message",
            "data": shift,
            "material_settings": settings,
            "material_pending": {}
        })
        user_states[user_id] = state
        reply_to_line(
            event,
            "本檔期帶出量已設定完成，不會重複設定。\n"
            "請選擇支出數量、入庫數量、餘量查詢或完成。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    state = user_states.get(user_id, {})
    state.update({
        "flow": "report_materials",
        "step": "waiting_material_initial",
        "data": shift,
        "material_settings": settings,
        "material_pending": {}
    })
    user_states[user_id] = state
    return handle_material_report_text(
        event,
        user_id,
        user_message
    )


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


def parse_ai_json(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("AI 回傳空白內容")
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError(f"AI 未回傳 JSON，原始內容：{cleaned[:300]}")
    return json.loads(match.group(0))


def parse_ai_text_fallback(text, result_type):
    content = str(text or "").strip()
    if result_type == "cups":
        match = re.search(
            r'(?:"?cups"?\s*:|總杯數|杯數|共計|合計)'
            r"\D{0,12}([1-9]\d{0,5})",
            content
        )
        if match:
            return {"cups": int(match.group(1)), "confidence": 0.5, "note": "文字備援擷取"}

    if result_type == "expense":
        amount_match = re.search(
            r'(?:"?amount"?\s*:|總額|總計|應付|實付|金額|合計)\D{0,15}'
            r"(?:NT\$|NTD|\$)?\s*"
            r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)",
            content,
            flags=re.IGNORECASE
        )
        if amount_match:
            description_match = re.search(
                r'"description"\s*:\s*"([^"\r\n]{1,100})',
                content,
                flags=re.IGNORECASE
            )
            return {
                "description": (
                    description_match.group(1).strip()
                    if description_match
                    else "收據支出（待確認）"
                ),
                "amount": amount_match.group(1),
                "note": "不完整 JSON 備援擷取",
                "confidence": 0.5
            }

    raise ValueError(f"無法從 AI 文字回應擷取資料：{content[:300]}")


def analyze_image_as_json(image_path, prompt, result_type):
    image_file = genai.upload_file(image_path)
    response = model.generate_content(
        [prompt, image_file],
        generation_config={
            "temperature": 0,
            "max_output_tokens": 512,
            "response_mime_type": "application/json"
        }
    )
    raw_text = response.text if response.text else ""
    print("[AI] 圖片辨識原始回應:", raw_text)

    try:
        return parse_ai_json(raw_text)
    except Exception as first_error:
        print("[AI] 第一次 JSON 解析失敗:", first_error)

    retry_prompt = (
        prompt
        + "\n你上一次沒有依格式回答。請重新查看同一張圖片，"
        "這次只能輸出一個合法 JSON 物件，不要使用 Markdown、說明文字或程式碼區塊。"
    )
    retry_response = model.generate_content(
        [retry_prompt, image_file],
        generation_config={
            "temperature": 0,
            "max_output_tokens": 512
        }
    )
    retry_text = retry_response.text if retry_response.text else ""
    print("[AI] 圖片辨識重試回應:", retry_text)
    try:
        return parse_ai_json(retry_text)
    except Exception as second_error:
        print("[AI] 第二次 JSON 解析失敗:", second_error)
        fallback_errors = []
        for fallback_text in (retry_text, raw_text):
            if not fallback_text:
                continue
            try:
                return parse_ai_text_fallback(fallback_text, result_type)
            except Exception as fallback_error:
                fallback_errors.append(str(fallback_error))
        raise ValueError("；".join(fallback_errors) or "AI 回應無法擷取")


def handle_cup_photo_result(event, user_id, image_path, message_id):
    state = user_states.get(user_id, {})
    cup_report = state.setdefault("cup_report", {})
    cup_report["photo_message_id"] = message_id
    shift = state.get("data", {})
    try:
        drive_file = upload_photo_to_drive(
            image_path,
            CUP_PHOTO_FOLDER_ID,
            "杯數照片",
            shift.get("name", user_id),
            message_id
        )
        cup_report["photo_drive_url"] = drive_file["url"]
        print("[DRIVE] 杯數照片已上傳:", drive_file)
    except Exception as e:
        print("[ERROR] 杯數照片上傳 Drive 失敗:", e)

    try:
        result = analyze_image_as_json(
            image_path,
            """辨識這張收銀機畫面或手寫紀錄中的「總杯數」。
只回傳 JSON：
{"cups": 正整數或null, "confidence": 0到1, "note": "簡短說明"}
不要把金額、訂單編號或日期當成杯數。無法確定時 cups 回傳 null。""",
            "cups"
        )
        print("[AI] 杯數照片辨識結果:", result)
        cups_text = str(result.get("cups", "")).strip().replace(",", "")
        if not re.fullmatch(r"[1-9]\d*", cups_text):
            raise ValueError("找不到可靠的正整數杯數")
        cups = int(cups_text)

        cup_report["ai_cups"] = cups
        cup_report["photo_message_id"] = message_id
        state["step"] = "waiting_cup_ai_confirm"
        user_states[user_id] = state
        reply_to_line(
            event,
            f"AI 辨識杯數：{cups} 杯\n請確認是否正確。",
            quick_reply=ai_result_quick_reply()
        )
    except Exception as e:
        print("[ERROR] 杯數照片辨識失敗:", e)
        state["step"] = "waiting_cup_count"
        user_states[user_id] = state
        reply_to_line(
            event,
            "杯數照片辨識失敗，請直接輸入正確杯數（大於 0 的正整數）。"
        )


def handle_expense_photo_result(event, user_id, image_path, message_id):
    state = user_states.get(user_id, {})
    expense_report = state.setdefault("expense_report", {})
    expense_report["has_receipt"] = True
    expense_report["photo_message_id"] = message_id
    shift = state.get("data", {})
    try:
        drive_file = upload_photo_to_drive(
            image_path,
            EXPENSE_PHOTO_FOLDER_ID,
            "費用收據",
            shift.get("name", user_id),
            message_id
        )
        expense_report["photo_drive_url"] = drive_file["url"]
        print("[DRIVE] 費用收據已上傳:", drive_file)
    except Exception as e:
        print("[ERROR] 費用收據上傳 Drive 失敗:", e)

    try:
        result = analyze_image_as_json(
            image_path,
            """辨識這張台灣收據、發票、電子發票證明聯或手寫付款憑證。
圖片可能橫放或旋轉，請先自行判斷正確閱讀方向。
只回傳 JSON：
{"description": "支出品項或商店名稱", "amount": 數字或null,
 "note": "收據上的簡短備註", "confidence": 0到1}
amount 必須是收據最終應付總額，不要使用統編、發票號碼、日期或找零。
如果只看得出總額但看不出商店或品項，description 回傳「收據支出」。
金額無法確定時 amount 回傳 null。""",
            "expense"
        )
        print("[AI] 收據照片辨識結果:", result)
        description = str(result.get("description", "")).strip()
        if not description or description.lower() == "null":
            description = "收據支出（待確認）"
        amount = parse_expense_amount(result.get("amount", ""))
        if amount is None:
            raise ValueError(f"找不到可靠的支出總額，AI結果：{result}")

        expense_report.update({
            "description": description[:100],
            "amount": amount,
            "has_receipt": True,
            "photo_message_id": message_id,
            "receipt_note": str(result.get("note", "")).strip()[:100],
            "no_receipt_reason": ""
        })
        state["step"] = "waiting_expense_ai_confirm"
        user_states[user_id] = state
        reply_to_line(
            event,
            "AI 辨識結果：\n"
            f"支出說明：{expense_report['description']}\n"
            f"支出費用：NT${format_expense_amount(expense_report['amount'])}\n"
            "請確認是否正確。",
            quick_reply=ai_result_quick_reply()
        )
    except Exception as e:
        print("[ERROR] 收據照片辨識失敗:", e)
        state["step"] = "waiting_expense_description"
        user_states[user_id] = state
        reply_to_line(
            event,
            "收據辨識失敗，但照片已保留。\n"
            "已切換為手動填寫，請輸入支出說明。"
        )


def handle_event_photo_result(event, user_id, image_path, message_id):
    state = user_states.get(user_id, {})
    event_report = state.setdefault("event_report", {})
    event_report["photo_message_id"] = message_id
    context = get_event_context(user_id)
    try:
        drive_file = upload_photo_to_drive(
            image_path,
            EVENT_PHOTO_FOLDER_ID,
            "事件照片",
            context.get("name", user_id),
            message_id
        )
        event_report["photo_drive_url"] = drive_file["url"]
        print("[DRIVE] 事件照片已上傳:", drive_file)
    except Exception as e:
        print("[ERROR] 事件照片上傳 Drive 失敗:", e)

    submit_event_record(event, user_id, state)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    state = user_states.get(user_id, {})
    if state.get("flow") == "report_mileage":
        reply_to_line(event, "里程回報請直接輸入儀表板公里數，不需要上傳照片。")
        return

    if (
        state.get("flow") == "report_cups"
        and state.get("step") != "waiting_cup_photo"
    ):
        reply_to_line(
            event,
            "目前不是等待杯數照片的步驟，請依照 Bot 提示操作。"
        )
        return

    if (
        state.get("flow") == "report_expense"
        and state.get("step") != "waiting_expense_photo"
    ):
        reply_to_line(
            event,
            "目前不是等待收據照片的步驟，請依照 Bot 提示操作。"
        )
        return

    if (
        state.get("flow") == "report_event"
        and state.get("step") != "waiting_event_photo"
    ):
        reply_to_line(
            event,
            "目前不是等待事件照片的步驟，請依照 Bot 提示操作。"
        )
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

        if state.get("flow") == "report_cups":
            handle_cup_photo_result(event, user_id, image_path, message_id)
            return

        if state.get("flow") == "report_expense":
            handle_expense_photo_result(event, user_id, image_path, message_id)
            return

        if state.get("flow") == "report_event":
            handle_event_photo_result(event, user_id, image_path, message_id)
            return

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
        start_event_flow(event, user_id)
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

    if data == "action=report_expense":
        if not require_shift_confirmed(event, user_id):
            return
        start_expense_report_flow(event, user_id)
        return

    if data == "action=report_materials":
        if not require_shift_confirmed(event, user_id):
            return
        start_material_report_flow(event, user_id)
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

    if handle_material_template_recovery(event, user_id, user_message):
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
