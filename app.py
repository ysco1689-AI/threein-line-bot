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

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


def get_google_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )

    return gspread.authorize(credentials)


def get_sheet_records():
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
    return sheet.get_all_records()


def get_users_records():
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("users")
    return sheet.get_all_records()


def get_qa_records():
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("qa")
    return sheet.get_all_records()


def get_user_role(user_id):
    users = get_users_records()

    for user in users:
        if str(user.get("line_user_id", "")).strip() == user_id:
            return {
                "role": str(user.get("role", "")).strip(),
                "status": str(user.get("status", "")).strip()
            }

    return {
        "role": "guest",
        "status": "pending"
    }


def reply_to_line(event, reply_text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )


def find_recipe(user_message):
    records = get_sheet_records()

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
    records = get_qa_records()

    for row in records:
        keywords = str(row.get("keywords", "")).split(",")
        answer = str(row.get("answer", "")).strip()
        permission = str(row.get("permission", "")).strip()

        matched = any(
            keyword.strip() and keyword.strip() in user_message
            for keyword in keywords
        )

        if matched:
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


def is_sensitive_question(user_message):
    sensitive_keywords = [
        "配方", "比例", "成本", "毛利", "原物料",
        "仙草汁", "黑糖", "二砂", "冬瓜糖", "甘草", "海鹽",
        "茶包", "煮法", "熬煮", "幾克", "幾公克", "多少克"
    ]

    return any(word in user_message for word in sensitive_keywords)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return "OK"


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

    if status == "blocked" or role == "blocked":
        reply_to_line(event, "此帳號目前無法使用本系統，請洽總部。")
        return

    allowed_recipe_roles = ["admin", "franchisee", "staff"]

    # 1. 先查配方
    recipe = find_recipe(user_message)

    if recipe:
        if status != "active" or role not in allowed_recipe_roles:
            reply_to_line(event, "此帳號目前沒有查詢配方權限，請洽總部確認。")
            return

        recipe_text = "\n".join(
            [f"{key}：{value}" for key, value in recipe.items() if value]
        )

        prompt = f"""
你是「三入好棧 AI 員工助手」。

請用繁體中文回答。
回答要簡單、現場可執行、不要太長。
你只能根據以下配方資料回答，不可以自行編造。

配方資料：
{recipe_text}

員工問題：
{user_message}
"""
        response = model.generate_content(prompt)
        reply_to_line(event, response.text)
        return

    # 2. 查不到配方，再查 Q&A
    qa_answer = find_qa_answer(user_message, role, status)

    if qa_answer:
        reply_to_line(event, qa_answer)
        return

    # 3. Q&A 查不到，但如果是敏感問題，不准 Gemini 亂回答
    if is_sensitive_question(user_message):
        reply_to_line(event, "此問題涉及內部資料，目前資料庫沒有明確答案，請洽總部確認。")
        return

    # 4. 最後才交給 Gemini 回一般設備、現場操作、服務問題
    prompt = f"""
你是「三入好棧 AI 員工助手」。

請用繁體中文回答。
回答要簡單、現場可執行、條列式，不要太長。

你可以回答：
1. 一般設備操作
2. 常見錯誤排除
3. 現場服務流程
4. 顧客應對
5. 基礎安全提醒

但以下內容不可回答：
1. 配方比例
2. 原物料成本
3. 毛利與內部財務
4. 未公開加盟政策
5. 任何你不確定的內容

如果你不確定，請回答：
「這個問題目前資料庫沒有明確資料，建議拍照或錄影後聯繫總部確認。」

員工問題：
{user_message}
"""

    response = model.generate_content(prompt)
    reply_to_line(event, response.text)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
