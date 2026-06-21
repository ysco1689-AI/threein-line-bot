import os
import re
import tempfile

import google.generativeai as genai
from flask import request
from linebot.v3.messaging import ApiClient, MessagingApiBlob
from linebot.v3.webhooks import ImageMessageContent, MessageEvent, PostbackEvent, TextMessageContent, VideoMessageContent

import state as app_state
from app import app, load_all_data
from config import MATERIAL_ALIASES, configuration, handler, model
from handlers.cup import available_cup_dates, handle_cup_photo_result, start_cup_report_flow, submit_cup_value
from handlers.event import handle_event_photo_result, start_event_flow
from handlers.expense import handle_expense_photo_result, parse_expense_amount, start_expense_report_flow, handle_expense_report_text
from handlers.material import handle_material_report_text, load_material_settings, get_shift_material_initials, start_material_report_flow
from handlers.mileage import start_mileage_report_flow
from handlers.router import handle_active_flow, handle_material_template_recovery
from handlers.shift import get_confirmed_shift, require_report_access, start_confirm_shift_flow
from services.ai import ask_gemini_text, classify_message, find_qa_answer, find_recipe
from services.intent import detect_report_intent
from services.messaging import reply_to_line
from services.users import get_user_role
from sheets.helpers import today_text


def extract_first_positive_int(message):
    match = re.search(r"[1-9]\d*", str(message or "").replace(",", ""))
    return int(match.group(0)) if match else None


def start_expense_from_intent(event, user_id, user_message):
    shift = get_confirmed_shift(user_id)
    if not shift:
        start_expense_report_flow(event, user_id)
        return

    dates = available_cup_dates(shift)
    selected_date = today_text()
    allowed_dates = {value.strftime("%Y/%m/%d") for value in dates}
    if selected_date not in allowed_dates:
        start_expense_report_flow(event, user_id)
        return

    amount = extract_first_positive_int(user_message)
    description = re.sub(r"(NTD|NT|\$|元|塊錢|支出|花費|花了|買了|付了)", "", user_message, flags=re.IGNORECASE)
    description = re.sub(r"\d+(?:\.\d{1,2})?", "", description).strip() or "支出"
    app_state.user_states[user_id] = {
        "flow": "report_expense",
        "step": "waiting_no_receipt_reason" if amount else "waiting_expense_amount",
        "data": shift,
        "expense_report": {
            "date": selected_date,
            "description": description[:100],
            "amount": amount,
            "has_receipt": False
        }
    }
    if amount:
        reply_to_line(
            event,
            f"已帶入支出：{description[:100]} / NT${amount}\n請輸入沒有收據或憑證的原因。"
        )
    else:
        reply_to_line(event, f"已帶入支出說明：{description[:100]}\n請輸入支出金額。")


def start_cup_from_intent(event, user_id, user_message):
    shift = get_confirmed_shift(user_id)
    cups = extract_first_positive_int(user_message)
    if not shift or not cups:
        start_cup_report_flow(event, user_id)
        return
    dates = available_cup_dates(shift)
    selected_date = today_text()
    allowed_dates = {value.strftime("%Y/%m/%d") for value in dates}
    if selected_date not in allowed_dates:
        start_cup_report_flow(event, user_id)
        return
    state = {
        "flow": "report_cups",
        "step": "waiting_cup_count",
        "data": shift,
        "cup_report": {"date": selected_date}
    }
    app_state.user_states[user_id] = state
    submit_cup_value(event, user_id, state, cups)


def start_material_from_intent(event, user_id, user_message):
    shift = get_confirmed_shift(user_id)
    if not shift:
        start_material_report_flow(event, user_id)
        return
    try:
        settings = load_material_settings()
        initials = get_shift_material_initials(shift)
    except Exception as e:
        print("[ERROR] 自然語言餘料啟動失敗:", e)
        start_material_report_flow(event, user_id)
        return
    if not initials:
        start_material_report_flow(event, user_id)
        return
    app_state.user_states[user_id] = {
        "flow": "report_materials",
        "step": "waiting_material_outbound",
        "data": shift,
        "material_settings": settings,
        "material_pending": {}
    }
    handle_material_report_text(event, user_id, user_message)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")

    if not signature:
        return "Missing signature", 400

    body = request.get_data(as_text=True)
    handler.handle(body, signature)

    return "OK"

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    state = app_state.user_states.get(user_id, {})
    if app_state.get("flow") == "report_mileage":
        reply_to_line(event, "里程回報請直接輸入儀表板公里數，不需要上傳照片。")
        return

    if (
        app_state.get("flow") == "report_cups"
        and app_state.get("step") != "waiting_cup_photo"
    ):
        reply_to_line(
            event,
            "目前不是等待杯數照片的步驟，請依照 Bot 提示操作。"
        )
        return

    if (
        app_state.get("flow") == "report_expense"
        and app_state.get("step") != "waiting_expense_photo"
    ):
        reply_to_line(
            event,
            "目前不是等待收據照片的步驟，請依照 Bot 提示操作。"
        )
        return

    if (
        app_state.get("flow") == "report_event"
        and app_state.get("step") != "waiting_event_photo"
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

        if app_state.get("flow") == "report_cups":
            handle_cup_photo_result(event, user_id, image_path, message_id)
            return

        if app_state.get("flow") == "report_expense":
            handle_expense_photo_result(event, user_id, image_path, message_id)
            return

        if app_state.get("flow") == "report_event":
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

    if role == "unknown":
        return

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
        if not require_report_access(event, user_id):
            return
        start_cup_report_flow(event, user_id)
        return

    if data == "action=report_mileage":
        if not require_report_access(event, user_id):
            return
        start_mileage_report_flow(event, user_id)
        return

    if data == "action=report_expense":
        if not require_report_access(event, user_id):
            return
        start_expense_report_flow(event, user_id)
        return

    if data == "action=report_materials":
        if not require_report_access(event, user_id):
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

    if role == "unknown":
        return

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

    intent_type, _matched_material = detect_report_intent(user_message, MATERIAL_ALIASES)
    if intent_type == "expense":
        if not require_report_access(event, user_id):
            return
        start_expense_from_intent(event, user_id, user_message)
        return
    if intent_type == "cup":
        if not require_report_access(event, user_id):
            return
        start_cup_from_intent(event, user_id, user_message)
        return
    if intent_type == "material":
        if not require_report_access(event, user_id):
            return
        start_material_from_intent(event, user_id, user_message)
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
