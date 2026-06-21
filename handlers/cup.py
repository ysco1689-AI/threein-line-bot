import re

import state as app_state
from config import CUP_PHOTO_FOLDER_ID
from handlers.shift import get_confirmed_shift
from services.ai import analyze_image_as_json
from services.messaging import reply_to_line
from services.photo import upload_photo_to_drive
from sheets.client import get_shift_spreadsheet
from sheets.helpers import normalize_date_text, now_time_text, parse_date, today_text
from linebot.v3.messaging import QuickReply, QuickReplyItem, MessageAction


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

    last_available_date = min(end_date + timedelta(days=1), today)
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

    state = app_state.user_states.get(user_id, {})
    state.update({
        "flow": "report_cups",
        "step": "waiting_cup_date",
        "data": shift,
        "cup_report": {}
    })
    app_state.user_states[user_id] = state

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
    state = app_state.user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("cup_report", None)
    app_state.user_states[user_id] = state

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
    app_state.user_states[user_id] = state
    reply_to_line(
        event,
        f"⚠️ 您 {report_date} 已填寫過杯數：{existing_cups} 杯\n"
        f"您現在輸入：{cups} 杯\n"
        "數字不同，確認要修改嗎？",
        quick_reply=cup_update_quick_reply()
    )
    return True

def handle_cup_report_text(event, user_id, user_message):
    state = app_state.user_states.get(user_id, {})
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
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            f"請選擇 {selected_date} 的杯數填寫方式：",
            quick_reply=input_method_quick_reply()
        )
        return True

    if step == "waiting_cup_input_method":
        if message in ["📷 拍照辨識", "拍照辨識", "拍照"]:
            state["step"] = "waiting_cup_photo"
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                "請上傳收銀機畫面或手寫杯數紀錄照片。\n"
                "請讓杯數數字清楚、完整入鏡。"
            )
            return True
        if message in ["✏️ 手動輸入", "手動輸入", "手動"]:
            state["step"] = "waiting_cup_count"
            app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
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

def handle_cup_photo_result(event, user_id, image_path, message_id):
    state = app_state.user_states.get(user_id, {})
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
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            f"AI 辨識杯數：{cups} 杯\n請確認是否正確。",
            quick_reply=ai_result_quick_reply()
        )
    except Exception as e:
        print("[ERROR] 杯數照片辨識失敗:", e)
        state["step"] = "waiting_cup_count"
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            "杯數照片辨識失敗，請直接輸入正確杯數（大於 0 的正整數）。"
        )
