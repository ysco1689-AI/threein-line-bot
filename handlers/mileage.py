import re

import state as app_state
from handlers.shift import get_confirmed_shift
from services.messaging import reply_to_line
from sheets.client import get_shift_spreadsheet
from sheets.helpers import get_records_by_header, normalize_date_text, now_time_text, today_text
from linebot.v3.messaging import QuickReply, QuickReplyItem, MessageAction


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
        state = app_state.user_states.get(user_id, {})
        state.update({
            "flow": "report_mileage",
            "step": "waiting_existing_plate_last_four",
            "data": shift,
            "mileage_report": {"existing_record": missing_plate_record}
        })
        app_state.user_states[user_id] = state
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
        state = app_state.user_states.get(user_id, {})
        state.update({
            "flow": "report_mileage",
            "step": "waiting_mileage_action",
            "data": shift,
            "mileage_report": {"today_records": records}
        })
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            f"今日里程紀錄：\n{summary}\n\n請選擇操作：",
            quick_reply=mileage_manage_quick_reply()
        )
        return

    state = app_state.user_states.get(user_id, {})
    state.update({
        "flow": "report_mileage",
        "step": "waiting_drive_status",
        "data": shift,
        "mileage_report": {}
    })
    app_state.user_states[user_id] = state
    reply_to_line(
        event,
        "今日是否有開車前往？",
        quick_reply=drive_quick_reply()
    )

def finish_mileage_flow(user_id):
    state = app_state.user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("mileage_report", None)
    app_state.user_states[user_id] = state

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
    state = app_state.user_states.get(user_id, {})
    step = state.get("step")
    shift = state.get("data", {})
    mileage_report = app_state.setdefault("mileage_report", {})
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
            app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                f"車牌 {plate_last_four} 今日已有紀錄，已切換為修改模式。\n"
                "請輸入新的出發里程。"
            )
            return True

        mileage_report["plate_last_four"] = plate_last_four
        mileage_report.setdefault("mode", "add")
        state["step"] = "waiting_start_mileage"
        app_state.user_states[user_id] = state
        reply_to_line(event, "請輸入出發里程（儀表板公里數）。")
        return True

    if step == "waiting_start_mileage":
        start_mileage = parse_mileage_number(message)
        if start_mileage is None:
            reply_to_line(event, "出發里程必須是 0 以上的數字，請重新輸入。")
            return True

        mileage_report["start_mileage"] = start_mileage
        state["step"] = "waiting_end_mileage"
        app_state.user_states[user_id] = state
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
