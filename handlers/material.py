import re

import gspread
import state as app_state
from config import MATERIAL_ALIASES, MATERIAL_COMPLETION_HEADERS, MATERIAL_SETUP_LABELS
from handlers.shift import get_confirmed_shift
from services.messaging import reply_to_line
from sheets.client import get_shift_spreadsheet
from sheets.helpers import get_records_by_header, normalize_date_text, now_time_text, parse_date, today_text
from linebot.v3.messaging import QuickReply, QuickReplyItem, MessageAction


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

    state = app_state.user_states.get(user_id, {})
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
    app_state.user_states[user_id] = state
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
    state = app_state.user_states.get(user_id, {})
    state.pop("flow", None)
    state.pop("step", None)
    state.pop("material_settings", None)
    state.pop("material_pending", None)
    app_state.user_states[user_id] = state

def write_material_completion(user_id, shift):
    spreadsheet = get_shift_spreadsheet()
    try:
        sheet = spreadsheet.worksheet("餘料完成紀錄")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title="餘料完成紀錄",
            rows=1000,
            cols=len(MATERIAL_COMPLETION_HEADERS)
        )
        sheet.append_row(MATERIAL_COMPLETION_HEADERS)

    rows = get_records_by_header(sheet, "LINE_ID")
    already_completed = any(
        str(row.get("LINE_ID", "")).strip() == user_id
        and normalize_date_text(row.get("日期", "")) == today_text()
        and str(row.get("檔期名稱", "")).strip()
        == str(shift.get("shift_name", "")).strip()
        and str(row.get("攤位編號", "")).strip()
        == str(shift.get("booth", "")).strip()
        for row in rows
    )
    if already_completed:
        return
    sheet.append_row([
        today_text(),
        user_id,
        shift.get("name", ""),
        shift.get("shift_name", ""),
        shift.get("booth", ""),
        now_time_text()
    ])

def handle_material_report_text(event, user_id, user_message):
    state = app_state.user_states.get(user_id, {})
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
        try:
            write_material_completion(user_id, shift)
        except Exception as e:
            print("[ERROR] 寫入餘料完成紀錄失敗:", e)
            reply_to_line(event, "記錄餘料完成狀態時發生問題，請稍後再試。")
            return True
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
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                "本檔期帶出量已設定完成，不需要再次設定。\n"
                "請選擇支出、入庫、餘量查詢或完成。",
                quick_reply=material_continue_quick_reply()
            )
            return True
        state["step"] = "waiting_material_initial"
        app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
        return True

    if step == "waiting_material_overuse_confirm":
        if message in ["❌ 取消餘料", "取消餘料", "取消"]:
            state["step"] = "waiting_material_message"
            state["material_pending"] = {}
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            reply_text,
            quick_reply=material_continue_quick_reply()
        )
        return True

    if message in ["➖ 支出數量", "支出數量"]:
        state["step"] = "waiting_material_outbound"
        state["material_pending"] = {}
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            "➖ 請輸入支出的品項與數量，例如：仙甘4包。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    if message in ["➕ 入庫數量", "入庫數量"]:
        state["step"] = "waiting_material_inbound"
        state["material_pending"] = {}
        app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
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
    app_state.user_states[user_id] = state
    action_text = "已入庫" if is_inbound else "已支出"
    reply_to_line(
        event,
        f"📦 {setting['name']}{action_text} {quantity} {setting['unit']}，"
        f"目前餘量 {new_remaining} {setting['unit']}。",
        quick_reply=material_continue_quick_reply()
    )
    return True
