import os
import json
import google.generativeai as genai
import gspread

from google.oauth2.service_account import Credentials
from flask import Flask, request

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

# =========================
# 建立 Flask 網頁伺服器
# =========================

app = Flask(__name__)

# =========================
# 全域快取資料
# =========================

recipe_cache = []
users_cache = []
qa_cache = []

# =========================
# Render 環境變數
# =========================

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# =========================
# Gemini 初始化
# =========================

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# =========================
# LINE BOT 初始化
# =========================

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# =========================
# Google Sheet 連線
# =========================

def get_google_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    service_account_info = json.loads(
        GOOGLE_SERVICE_ACCOUNT_JSON
    )

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )

    return gspread.authorize(credentials)

# =========================
# 載入 Google Sheet
# =========================

def load_all_data():
    global recipe_cache, users_cache, qa_cache

    try:
        gc = get_google_client()

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        # 配方表
        recipe_cache = spreadsheet.sheet1.get_all_records()

        # users 表
        users_cache = spreadsheet.worksheet("users").get_all_records()

        # qa 表
        qa_cache = spreadsheet.worksheet("qa").get_all_records()

        print("Google Sheet 資料載入成功")

    except Exception as e:
        print("讀取 Google Sheet 失敗：", e)

# =========================
# 自動新增新使用者
# =========================

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

# =========================
# 查詢使用者權限
# =========================

def get_user_role(user_id):
    users = users_cache

    for user in users:
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

# =========================
# LINE 回覆功能
# =========================

def reply_to_line(event, reply_text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# =========================
# 搜尋配方
# =========================

def find_recipe(user_message):
    records = recipe_cache

    want_hot = any(
        word in user_message
        for word in ["熱", "熱的", "熱飲", "溫的"]
    )

    want_cold = any(
        word in user_message
        for word in ["冷", "冷的", "冷飲", "冰", "冰的"]
    )

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

# =========================
# 搜尋 QA 資料庫
# =========================

def find_qa_answer(user_message, role, status):
    records = qa_cache

    for row in records:

        keywords = str(
            row.get("keywords", "")
        ).split(",")

        answer = str(
            row.get("answer", "")
        ).strip()

        permission = str(
            row.get("permission", "")
        ).strip()

        # =========================
        # QA答案太短 → 視同無效
        # =========================

        if len(answer) < 20:
            continue

        # =========================
        # keyword 太短 → 不參與匹配
        # 避免「紅茶」「E05」亂命中
        # =========================

        matched = any(
            len(keyword.strip()) >= 3
            and keyword.strip() in user_message
            for keyword in keywords
        )

        if not matched:
            continue

        # =========================
        # public 權限
        # =========================

        if permission == "public":
            return answer

        # =========================
        # franchisee 權限
        # =========================

        if permission == "franchisee":

            if (
                status == "active"
                and role in ["admin", "franchisee", "staff"]
            ):
                return answer

            return "此問題需要加盟主或員工權限，請洽總部。"

        # =========================
        # admin 權限
        # =========================

        if permission == "admin":

            if (
                status == "active"
                and role == "admin"
            ):
                return answer

            return "此問題需要總部權限，請洽總部。"

    # =========================
    # 完全沒找到
    # =========================

    return None
# =========================
# 敏感問題阻擋
# =========================

def is_sensitive_question(user_message):
    sensitive_keywords = [
        "配方", "比例", "成本", "毛利", "原物料",
        "仙草汁", "黑糖", "二砂", "冬瓜糖",
        "甘草", "海鹽", "茶包", "煮法",
        "熬煮", "幾克", "幾公克", "多少克"
    ]

    return any(
        word in user_message
        for word in sensitive_keywords
    )

# =========================
# LINE webhook
# =========================

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")

    if not signature:
        return "Missing signature", 400

    body = request.get_data(as_text=True)

    handler.handle(body, signature)

    return "OK"

# =========================
# 主訊息處理區
# =========================

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

    # =========================
    # 黑名單阻擋
    # =========================

    if status == "blocked" or role == "blocked":
        reply_to_line(event, "此帳號目前無法使用本系統，請洽總部。")
        return

    # =========================
    # 第一優先：QA / SOP / 客訴
    # =========================

    qa_answer = find_qa_answer(user_message, role, status)

    if qa_answer:
        reply_to_line(event, qa_answer)
        return

    # =========================
    # 第二優先：查配方
    # =========================

    allowed_recipe_roles = [
        "admin",
        "franchisee",
        "staff"
    ]

    recipe = find_recipe(user_message)

    if recipe:

        if status != "active" or role not in allowed_recipe_roles:
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

    # =========================
    # 第三優先：敏感問題阻擋
    # =========================

    if is_sensitive_question(user_message):
        reply_to_line(
            event,
            "此問題涉及內部資料，目前資料庫沒有明確答案，請洽總部確認。"
        )
        return

    # =========================
    # 最後才交給 Gemini
    # QA 沒資料時，提供現場排除建議
    # =========================

    prompt = f"""
你是三入好棧現場店長助手。

請用繁體中文回答。
回答對象是現場員工，不是客人。

回答規則：
- 像資深店長教員工
- 設備問題請用3到5點條列
- 每點一句話，現場員工看得懂
- 不要只回代碼或單字
- 回答至少120字
- 一定要給「現場先做什麼」
- 不知道代碼真正意思時，要說「先用基本排除法」
- 禁止回答配方比例、成本、毛利、未公開加盟資訊、危險拆機

回答格式：
1. 可能原因：
2. 現場先做：
3. 檢查重點：
4. 如果還是異常：
5. 回報主管：

員工問題：
{user_message}
"""

    print("HIT_GEMINI")

    response = model.generate_content(
        prompt,
        generation_config={
            "temperature": 0.5,
            "max_output_tokens": 700,
            "top_p": 0.9,
            "top_k": 40
        }
    )

    print("GEMINI_TEXT:", response.text)
    print("GEMINI_LENGTH:", len(response.text) if response.text else 0)
    
    reply_to_line(
        event,
        response.text if response.text else "目前沒有明確答案，請先拍照回報主管。"
    )
    
# =========================
# 啟動時先載入 Google Sheet
# =========================

load_all_data()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
