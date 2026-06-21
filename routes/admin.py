import json
import os
from datetime import timedelta

import gspread
from flask import request, send_file

from app import app
from config import REMINDER_API_KEY, REMINDER_HEADERS
from services.messaging import push_message
from sheets.client import get_shift_spreadsheet
from sheets.helpers import get_records_by_header, normalize_date_text, now_time_text, parse_date, today_text


def get_reminder_sheet():
    spreadsheet = get_shift_spreadsheet()
    try:
        sheet = spreadsheet.worksheet("提醒紀錄")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title="提醒紀錄",
            rows=1000,
            cols=len(REMINDER_HEADERS)
        )
        sheet.append_row(REMINDER_HEADERS)
        return sheet
    if not sheet.get_all_values():
        sheet.append_row(REMINDER_HEADERS)
    return sheet

def get_today_shift_rows():
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("排班表")
    rows = get_records_by_header(sheet, "LINE_ID")
    today = parse_date(today_text())
    matches = []
    for row in rows:
        status = str(row.get("狀態", "啟用")).strip()
        start_date = parse_date(row.get("開始日期", ""))
        end_date = parse_date(row.get("結束日期", ""))
        user_id = str(row.get("LINE_ID", "")).strip()
        if (
            user_id
            and start_date
            and end_date
            and start_date <= today <= end_date
            and (not status or status in ["啟用", "active", "Active", "ACTIVE"])
        ):
            matches.append(row)
    return matches

def get_today_confirmed_ids():
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("確認紀錄")
    rows = get_records_by_header(sheet, "LINE_ID")
    return {
        str(row.get("LINE_ID", "")).strip()
        for row in rows
        if (
            normalize_date_text(row.get("日期", "")) == today_text()
            and str(row.get("確認狀態", "已確認")).strip() == "已確認"
        )
    }

def get_today_material_reported_ids():
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("餘料回報")
    rows = get_records_by_header(sheet, "LINE_ID")
    reported_ids = {
        str(row.get("LINE_ID", "")).strip()
        for row in rows
        if normalize_date_text(row.get("日期", "")) == today_text()
    }
    try:
        completion_sheet = spreadsheet.worksheet("餘料完成紀錄")
        completion_rows = get_records_by_header(completion_sheet, "LINE_ID")
        reported_ids.update(
            str(row.get("LINE_ID", "")).strip()
            for row in completion_rows
            if normalize_date_text(row.get("日期", "")) == today_text()
        )
    except gspread.WorksheetNotFound:
        pass
    return reported_ids

def get_sent_reminder_keys():
    sheet = get_reminder_sheet()
    rows = get_records_by_header(sheet, "LINE_ID")
    return {
        (
            normalize_date_text(row.get("日期", "")),
            str(row.get("LINE_ID", "")).strip(),
            str(row.get("提醒類型", "")).strip()
        )
        for row in rows
        if str(row.get("發送結果", "")).strip() == "成功"
    }

def log_reminder(user_id, reminder_type, result, error_message=""):
    sheet = get_reminder_sheet()
    sheet.append_row([
        today_text(),
        user_id,
        reminder_type,
        now_time_text(),
        result,
        str(error_message)[:300]
    ])

def send_reminders(reminder_type):
    shift_rows = get_today_shift_rows()
    sent_keys = get_sent_reminder_keys()
    if reminder_type == "未確認檔期":
        excluded_ids = get_today_confirmed_ids()
        candidates = {
            str(row.get("LINE_ID", "")).strip()
            for row in shift_rows
        } - excluded_ids
        message = "⏰ 提醒：您今日尚未完成檔期確認，請記得點選「✅ 確認檔期」。"
    elif reminder_type == "最後一天餘料":
        excluded_ids = get_today_material_reported_ids()
        candidates = {
            str(row.get("LINE_ID", "")).strip()
            for row in shift_rows
            if normalize_date_text(row.get("結束日期", "")) == today_text()
        } - excluded_ids
        message = "📦 提醒：今日為檔期最後一天，請記得完成餘料回報！"
    else:
        raise ValueError(f"不支援的提醒類型：{reminder_type}")

    result = {"candidates": len(candidates), "sent": 0, "skipped": 0, "failed": 0}
    for user_id in sorted(candidates):
        if not user_id:
            continue
        key = (today_text(), user_id, reminder_type)
        if key in sent_keys:
            result["skipped"] += 1
            continue
        try:
            push_message(user_id, message)
            log_reminder(user_id, reminder_type, "成功")
            result["sent"] += 1
        except Exception as e:
            print(f"[ERROR] {reminder_type}推播失敗 {user_id}:", e)
            log_reminder(user_id, reminder_type, "失敗", e)
            result["failed"] += 1
    return result

def run_scheduled_reminders(job):
    if job == "unconfirmed":
        return {"unconfirmed": send_reminders("未確認檔期")}
    if job == "materials":
        return {"materials": send_reminders("最後一天餘料")}
    if job == "all":
        return {
            "unconfirmed": send_reminders("未確認檔期"),
            "materials": send_reminders("最後一天餘料")
        }
    raise ValueError("job 必須是 unconfirmed、materials 或 all")

@app.route("/run-reminders", methods=["GET", "POST"])
def run_reminders_route():
    request_key = request.args.get("key", "")
    job = request.args.get("job", "all").strip().lower()
    if not REMINDER_API_KEY or request_key != REMINDER_API_KEY:
        return "Forbidden", 403
    try:
        result = run_scheduled_reminders(job)
        return json.dumps(result, ensure_ascii=False), 200, {
            "Content-Type": "application/json; charset=utf-8"
        }
    except Exception as e:
        print("[ERROR] 執行提醒失敗:", e)
        return f"提醒執行失敗：{e}", 500

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
