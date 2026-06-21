import re

import state as app_state
from config import EXPENSE_PHOTO_FOLDER_ID
from handlers.cup import ai_result_quick_reply, available_cup_dates, date_quick_reply, input_method_quick_reply
from handlers.shift import get_confirmed_shift
from services.ai import analyze_image_as_json
from services.messaging import reply_to_line
from services.photo import upload_photo_to_drive
from sheets.client import get_shift_spreadsheet
from sheets.helpers import get_records_by_header, normalize_date_text, now_time_text
from linebot.v3.messaging import QuickReply, QuickReplyItem, MessageAction


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

    state = app_state.user_states.get(user_id, {})
    app_state.update({
        "flow": "report_expense",
        "step": "waiting_expense_date",
        "data": shift,
        "expense_report": {}
    })
    app_state.user_states[user_id] = state

    date_text = " / ".join(value.strftime("%m/%d") for value in dates)
    reply_to_line(
        event,
        f"💰 請選擇支出日期：\n{date_text}",
        quick_reply=date_quick_reply(dates, "支出日期")
    )

def reset_expense_entry(user_id, keep_date=True):
    state = app_state.user_states.get(user_id, {})
    expense_report = app_state.get("expense_report", {})
    report_date = expense_report.get("date") if keep_date else None
    state["expense_report"] = {}
    if report_date:
        state["expense_report"]["date"] = report_date
    state["step"] = "waiting_expense_input_method" if report_date else "waiting_expense_date"
    app_state.user_states[user_id] = state

def finish_expense_flow(user_id):
    state = app_state.user_states.get(user_id, {})
    app_state.pop("flow", None)
    app_state.pop("step", None)
    app_state.pop("expense_report", None)
    app_state.user_states[user_id] = state

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
    shift = app_state.get("data", {})
    expense_report = app_state.get("expense_report", {})
    try:
        review_status = write_expense_record(user_id, shift, expense_report)
    except Exception as e:
        print("[ERROR] 寫入費用支出失敗:", e)
        reply_to_line(event, "寫入費用資料時發生問題，請稍後再試。")
        return True

    state["step"] = "waiting_expense_continue"
    app_state.user_states[user_id] = state
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
    state = app_state.user_states.get(user_id, {})
    step = app_state.get("step")
    shift = app_state.get("data", {})
    expense_report = app_state.setdefault("expense_report", {})
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
        app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                "請上傳收據或憑證照片。\n請讓店名、品項與總金額清楚入鏡。"
            )
            return True
        if message in ["✏️ 手動輸入", "手動輸入", "手動"]:
            expense_report["has_receipt"] = False
            state["step"] = "waiting_expense_description"
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇付款方式：",
                quick_reply=payment_method_quick_reply()
            )
        else:
            state["step"] = "waiting_no_receipt_reason"
            app_state.user_states[user_id] = state
            reply_to_line(event, "請輸入沒有收據或憑證的原因。")
        return True

    if step == "waiting_expense_ai_confirm":
        if message in ["✅ AI辨識正確", "AI辨識正確", "正確"]:
            state["step"] = "waiting_payment_method"
            app_state.user_states[user_id] = state
            reply_to_line(
                event,
                "請選擇付款方式：",
                quick_reply=payment_method_quick_reply()
            )
            return True
        if message in ["✏️ 修改AI結果", "修改AI結果", "修改"]:
            state["step"] = "waiting_expense_description"
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
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
            app_state.user_states[user_id] = state
            reply_to_line(event, "請說明其他付款方式。")
            return True
        expense_report["payment_note"] = ""
        state["step"] = "checking_expense_duplicate"
        app_state.user_states[user_id] = state

    if step == "waiting_payment_note":
        if len(message) < 2:
            reply_to_line(event, "請說明其他付款方式，至少 2 個字。")
            return True
        expense_report["payment_note"] = message[:100]
        state["step"] = "checking_expense_duplicate"
        app_state.user_states[user_id] = state

    if app_state.get("step") == "checking_expense_duplicate":
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
            app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
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

def handle_expense_photo_result(event, user_id, image_path, message_id):
    state = app_state.user_states.get(user_id, {})
    expense_report = app_state.setdefault("expense_report", {})
    expense_report["has_receipt"] = True
    expense_report["photo_message_id"] = message_id
    shift = app_state.get("data", {})
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
        app_state.user_states[user_id] = state
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
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            "收據辨識失敗，但照片已保留。\n"
            "已切換為手動填寫，請輸入支出說明。"
        )
