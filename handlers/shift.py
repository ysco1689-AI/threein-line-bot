import gspread
from datetime import timedelta

import state as app_state
from services.messaging import reply_to_line
from sheets.client import get_shift_spreadsheet
from sheets.helpers import get_records_by_header, normalize_date_text, now_time_text, parse_date, today_text
from linebot.v3.messaging import QuickReply, QuickReplyItem, MessageAction


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

def find_latest_started_shift(user_id):
    today = parse_date(today_text())
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("排班表")
    rows = get_records_by_header(sheet, "LINE_ID")
    matches = []
    for row in rows:
        if str(row.get("LINE_ID", "")).strip() != user_id:
            continue
        status = str(row.get("狀態", "啟用")).strip()
        if status and status not in ["啟用", "active", "Active", "ACTIVE"]:
            continue
        start_date = parse_date(row.get("開始日期", ""))
        end_date = parse_date(row.get("結束日期", ""))
        if not start_date or not end_date or start_date > today:
            continue
        matches.append((end_date, row))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    end_date, row = matches[0]
    return {
        "line_id": user_id,
        "name": str(row.get("姓名", "")).strip(),
        "shift_name": str(row.get("檔期名稱", "")).strip(),
        "booth": str(row.get("攤位編號", "")).strip(),
        "start_date": normalize_date_text(row.get("開始日期", "")),
        "end_date": end_date.strftime("%Y/%m/%d")
    }

def check_shift_deadline(shift):
    end_date = parse_date(shift.get("end_date", "")) if shift else None
    today = parse_date(today_text())
    return bool(end_date and today and today <= end_date + timedelta(days=1))

def require_report_access(event, user_id):
    try:
        latest_shift = find_latest_started_shift(user_id)
    except Exception as e:
        print("[ERROR] 檢查檔期截止時間失敗:", e)
        latest_shift = None
    if latest_shift and not check_shift_deadline(latest_shift):
        reply_to_line(event, "此檔期已關閉，如需修改請聯絡主管。")
        return False
    return require_shift_confirmed(event, user_id)

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
        app_state.user_states.pop(user_id, None)
        reply_to_line(event, "查無您今日的排班紀錄，請聯絡主管確認。")
        return

    app_state.user_states[user_id] = {
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
    app_state.user_states[user_id] = {
        "shift_confirmed": True,
        "confirmed_date": today_text(),
        "data": shift
    }

def check_shift_confirmed(user_id):
    state = app_state.user_states.get(user_id, {})
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
    state = app_state.user_states.get(user_id, {})
    shift = state.get("data")
    if shift:
        return shift

    shift = find_accessible_shift(user_id)
    if shift:
        state["data"] = shift
        app_state.user_states[user_id] = state
    return shift

def handle_confirm_shift_text(event, user_id, user_message):
    state = app_state.user_states.get(user_id, {})
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
        app_state.user_states.pop(user_id, None)
        reply_to_line(event, "已通知主管，請等候聯繫。如有緊急狀況請直接致電。")
        return True

    reply_to_line(event, "請選擇「✅ 確認」或「❌ 不是我的」。", quick_reply=confirm_quick_reply())
    return True
