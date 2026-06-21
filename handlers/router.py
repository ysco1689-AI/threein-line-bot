import re

import state as app_state
from handlers.cup import handle_cup_report_text
from handlers.event import handle_event_report_text
from handlers.expense import handle_expense_report_text
from handlers.material import get_shift_material_initials, handle_material_report_text, load_material_settings, material_continue_quick_reply, parse_material_initial_batch
from handlers.mileage import handle_mileage_report_text
from handlers.shift import check_shift_confirmed, check_shift_deadline, get_confirmed_shift, handle_confirm_shift_text
from services.messaging import reply_to_line


def handle_active_flow(event, user_id, user_message):
    state = app_state.user_states.get(user_id)
    if not state or not app_state.get("flow"):
        return False

    if app_state.get("flow") in [
        "report_cups",
        "report_mileage",
        "report_expense",
        "report_materials"
    ]:
        shift = app_state.get("data", {})
        if shift and not check_shift_deadline(shift):
            app_state.user_states.pop(user_id, None)
            reply_to_line(event, "此檔期已關閉，如需修改請聯絡主管。")
            return True

    if app_state.get("flow") == "confirm_shift" and app_state.get("step") == "waiting_confirm":
        return handle_confirm_shift_text(event, user_id, user_message)

    if app_state.get("flow") == "report_cups":
        return handle_cup_report_text(event, user_id, user_message)

    if app_state.get("flow") == "report_mileage":
        return handle_mileage_report_text(event, user_id, user_message)

    if app_state.get("flow") == "report_expense":
        return handle_expense_report_text(event, user_id, user_message)

    if app_state.get("flow") == "report_materials":
        return handle_material_report_text(event, user_id, user_message)

    if app_state.get("flow") == "report_event":
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
        state = app_state.user_states.get(user_id, {})
        app_state.update({
            "flow": "report_materials",
            "step": "waiting_material_message",
            "data": shift,
            "material_settings": settings,
            "material_pending": {}
        })
        app_state.user_states[user_id] = state
        reply_to_line(
            event,
            "本檔期帶出量已設定完成，不會重複設定。\n"
            "請選擇支出數量、入庫數量、餘量查詢或完成。",
            quick_reply=material_continue_quick_reply()
        )
        return True

    state = app_state.user_states.get(user_id, {})
    app_state.update({
        "flow": "report_materials",
        "step": "waiting_material_initial",
        "data": shift,
        "material_settings": settings,
        "material_pending": {}
    })
    app_state.user_states[user_id] = state
    return handle_material_report_text(
        event,
        user_id,
        user_message
    )
