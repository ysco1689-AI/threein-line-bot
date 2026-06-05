import os
import json
import tempfile
import re
import google.generativeai as genai
import gspread

from google.oauth2.service_account import Credentials
from flask import Flask, request

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

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHIFT_SPREADSHEET_ID = os.getenv("SHIFT_SPREADSHEET_ID", SPREADSHEET_ID)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


def get_google_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )
    return gspread.authorize(credentials)


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


def normalize_date_text(value):
    text = str(value).strip()
    if not text:
        return ""

    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue

    return text


def today_text():
    from datetime import datetime, timezone, timedelta
    taipei_tz = timezone(timedelta(hours=8))
    return datetime.now(taipei_tz).strftime("%Y/%m/%d")


def now_time_text():
    from datetime import datetime, timezone, timedelta
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


def find_today_shift(user_id):
    today = today_text()
    spreadsheet = get_shift_spreadsheet()
    sheet = spreadsheet.worksheet("排班表")
    rows = get_records_by_header(sheet, "LINE_ID")

    for row in rows:
        line_id = str(row.get("LINE_ID", "")).strip()
        status = str(row.get("狀態", "啟用")).strip()
        start_date = normalize_date_text(row.get("開始日期", ""))
        end_date = normalize_date_text(row.get("結束日期", ""))

        if line_id != user_id:
            continue

        if status and status not in ["啟用", "active", "Active", "ACTIVE"]:
            continue

        if start_date <= today <= end_date:
            return {
                "line_id": line_id,
                "name": str(row.get("姓名", "")).strip(),
                "shift_name": str(row.get("檔期名稱", "")).strip(),
                "booth": str(row.get("攤位編號", "")).strip(),
                "start_date": start_date,
                "end_date": end_date
            }

    return None


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
    return (
        state.get("shift_confirmed") is True
        and state.get("confirmed_date") == today_text()
    )


def require_shift_confirmed(event, user_id):
    if check_shift_confirmed(user_id):
        return True

    reply_to_line(event, "請先點選「✅ 確認檔期」完成今日確認後，才能使用此功能。")
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
    if not state or state.get("shift_confirmed"):
        return False

    if state.get("flow") == "confirm_shift" and state.get("step") == "waiting_confirm":
        return handle_confirm_shift_text(event, user_id, user_message)

    return False


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

    if not admin_key:
        return "ADMIN_SETUP_KEY is not configured", 403

    if request_key != admin_key:
        return "Forbidden", 403

    try:
        from setup_richmenu import (
            create_rich_menu,
            create_richmenu_image,
            require_token,
            set_default_rich_menu,
            upload_rich_menu_image,
        )

        require_token()
        image_path = create_richmenu_image()
        rich_menu_id = create_rich_menu()
        upload_rich_menu_image(rich_menu_id, image_path)
        set_default_rich_menu(rich_menu_id)
        return f"Rich Menu 建立完成：{rich_menu_id}", 200

    except Exception as e:
        print("[ERROR] setup-richmenu 失敗:", e)
        return f"Rich Menu 建立失敗：{e}", 500


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id
    image_path = None

    try:
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            image_content = line_bot_blob_api.get_message_content(message_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_file.write(image_content)
            image_path = temp_file.name

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
        reply_to_line(event, "📋 事件紀錄功能會在後續階段開放，目前第一階段先完成確認檔期。")
        return

    locked_actions = {
        "action=report_cups": "🥤 杯數回報",
        "action=report_materials": "📦 餘料回報",
        "action=report_expense": "💰 費用支出",
        "action=report_mileage": "🚗 里程回報"
    }

    if data in locked_actions:
        if not require_shift_confirmed(event, user_id):
            return

        reply_to_line(event, f"{locked_actions[data]}功能會在後續階段開放，目前第一階段先完成確認檔期。")
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
