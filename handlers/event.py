import re
from datetime import timedelta

import gspread
import state as app_state
from config import EVENT_HEADERS, EVENT_PHOTO_FOLDER_ID, EVENT_TYPES
from handlers.shift import find_accessible_shift
from services.messaging import reply_to_line
from services.photo import upload_photo_to_drive
from sheets.client import get_shift_spreadsheet
from sheets.helpers import get_records_by_header, normalize_date_text, now_time_text, parse_date, today_text
from linebot.v3.messaging import QuickReply, QuickReplyItem, MessageAction


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
    state = app_state.user_states.get(user_id, {})
    app_state.pop("flow", None)
    app_state.pop("step", None)
    app_state.pop("event_report", None)
    app_state.user_states[user_id] = state

def start_event_flow(event, user_id):
    state = app_state.user_states.get(user_id, {})
    app_state.update({
        "flow": "report_event",
        "step": "waiting_event_action",
        "event_report": {}
    })
    app_state.user_states[user_id] = state
    reply_to_line(
        event,
        "📋 請選擇事件紀錄操作：",
        quick_reply=event_action_quick_reply()
    )

def submit_event_record(event, user_id, state):
    event_report = app_state.get("event_report", {})
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
    state = app_state.user_states.get(user_id, {})
    step = app_state.get("step")
    event_report = app_state.setdefault("event_report", {})
    message = user_message.strip()

    if message in ["取消事件", "取消", "退出"]:
        finish_event_flow(user_id)
        reply_to_line(event, "已取消事件紀錄。")
        return True

    if step == "waiting_event_action":
        if message in ["📝 新增事件", "新增事件"]:
            state["step"] = "waiting_event_type"
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇事件類型：",
                quick_reply=event_type_quick_reply()
            )
            return True
        if message in ["🔍 查詢歷史", "查詢歷史"]:
            state["step"] = "waiting_event_filter"
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
        reply_to_line(event, "請描述事件內容。")
        return True

    if step == "waiting_event_description":
        if len(message) < 2:
            reply_to_line(event, "事件說明至少需要 2 個字，請重新輸入。")
            return True
        event_report["description"] = message[:500]
        state["step"] = "waiting_event_photo_choice"
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            "是否需要附上照片？",
            quick_reply=event_photo_quick_reply()
        )
        return True

    if step == "waiting_event_photo_choice":
        if message in ["📷 上傳照片", "上傳照片", "照片"]:
            state["step"] = "waiting_event_photo"
            app_state.user_states[user_id] = state
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

def handle_event_photo_result(event, user_id, image_path, message_id):
    state = app_state.user_states.get(user_id, {})
    event_report = app_state.setdefault("event_report", {})
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
