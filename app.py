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
# Render 會透過這個接收 LINE webhook
# =========================

app = Flask(__name__)


# =========================
# 從 Render 環境變數取得設定
# 不會把敏感金鑰直接寫死在程式
# =========================

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")


# =========================
# 初始化 Gemini AI 模型
# =========================

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")


# =========================
# 初始化 LINE BOT API
# =========================

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# =========================
# 建立 Google Sheet 連線
# 之後所有資料表都透過這裡存取
# =========================

def get_google_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )

    return gspread.authorize(credentials)


# =========================
# 讀取配方資料表 sheet1
# =========================

def get_sheet_records():
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
    return sheet.get_all_records()


# =========================
# 讀取 users 權限資料表
# =========================

def get_users_records():
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("users")
    return sheet.get_all_records()


# =========================
# 讀取 qa 問答資料表
# =========================

def get_qa_records():
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("qa")
    return sheet.get_all_records()


# =========================
# 自動新增新使用者
# users 表找不到時，預設新增為 guest / pending
# =========================

def add_new_user(user_id):
    gc = get_google_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("users")

    sheet.append_row([
        user_id,
        "新使用者",
        "guest",
        "pending",
        "自動加入"
    ])

    print("已自動新增使用者:", user_id)


# =========================
# 根據 LINE user_id
# 查詢此人的角色與狀態
# 如果 users 表找不到，就自動新增成 guest / pending
# =========================

def get_user_role(user_id):
    users = get_users_records()

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
# 統一 LINE 回覆功能
# 避免重複程式碼
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
# 根據使用者訊息搜尋配方
# 並判斷冷熱飲
# =========================

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


# =========================
# 搜尋 qa 問答資料庫
# 根據 keywords 比對問題
# 並依 permission 判斷是否能回答
# =========================

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


# =========================
# 防止 Gemini 回答內部敏感資訊
# 避免查不到配方時，AI 自己亂猜比例或成本
# =========================

def is_sensitive_question(user_message):
    sensitive_keywords = [
        "配方", "比例", "成本", "毛利", "原物料",
        "仙草汁", "黑糖", "二砂", "冬瓜糖", "甘草", "海鹽",
        "茶包", "煮法", "熬煮", "幾克", "幾公克", "多少克"
    ]

    return any(word in user_message for word in sensitive_keywords)


# =========================
# LINE webhook 接收入口
# LINE 傳訊息時會進到這裡
# =========================

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    handler.handle(body, signature)

    return "OK"


# =========================
# 主訊息處理區
# 整個 AI LINE Bot 核心流程
# =========================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    print("LINE_USER_ID:", user_id)
    print("USER_MESSAGE:", user_message)

    # 取得使用者權限
    user_info = get_user_role(user_id)
    role = user_info["role"]
    status = user_info["status"]

    print("ROLE:", role)
    print("STATUS:", status)

    # =========================
    # 黑名單直接禁止使用
    # =========================

    if status == "blocked" or role == "blocked":
        reply_to_line(event, "此帳號目前無法使用本系統，請洽總部。")
        return

    # =========================
    # 第一優先：查詢配方
    # admin / franchisee / staff 且 active 才能查配方
    # =========================

    allowed_recipe_roles = ["admin", "franchisee", "staff"]

    recipe = find_recipe(user_message)

    if recipe:
        if status != "active" or role not in allowed_recipe_roles:
            reply_to_line(event, "此帳號目前沒有查詢配方權限，請洽總部確認。")
            return

        recipe_text = "\n".join(
            [f"{key}：{value}" for key, value in recipe.items() if value]
        )

        prompt = f"""
你是「三入好棧 AI 專業店員助手」。

請用繁體中文回答。

你的回答風格要像：
1. 專業飲料店店員
2. 有經驗的現場服務員
3. 對客人有禮貌
4. 不要太生硬
5. 不要只像機器直接貼資料

回答原則：
1. 先正常口語回答
2. 再提供重點說明
3. 可適度加入服務語氣
4. 不要過度冗長
5. 不要亂修改配方內容

如果配方本身有固定規定：
請用「專業店員口吻」解釋原因。

例如：
「仙草甘茶甜度目前是固定設定，因為這款比例是依照仙草風味與甜度平衡設計，調整後容易影響整體口感，還請見諒。」

不要只回答：
「不能調整。」

你只能根據以下配方資料回答，不可以自行編造。

配方資料：
{recipe_text}

員工問題：
{user_message}
"""

        response = model.generate_content(prompt)
        reply_to_line(event, response.text)
        return

    # =========================
    # 第二優先：查詢 qa 問答資料庫
    # 適合放檔期、叫貨、設備、SOP 等固定答案
    # =========================

    qa_answer = find_qa_answer(user_message, role, status)

    if qa_answer:
        reply_to_line(event, qa_answer)
        return

    # =========================
    # 第三優先：敏感問題阻擋
    # 配方表與 Q&A 都查不到時，避免 Gemini 亂回答內部機密
    # =========================

    if is_sensitive_question(user_message):
        reply_to_line(event, "此問題涉及內部資料，目前資料庫沒有明確答案，請洽總部確認。")
        return

    # =========================
    # 最後才交給 Gemini 回答
    # 只處理一般設備、服務、現場問題
    # =========================

    prompt = f"""
你是「三入好棧 AI 現場店長助手」。

請用繁體中文回答。

你的回答風格要像：
1. 有經驗的夜市店長
2. 現場主管
3. 教員工排除問題

回答方式：
1. 先安撫對方
2. 再一步一步教學
3. 使用條列式
4. 回答務實、現場能立即操作
5. 不要太官方
6. 不要太簡短

你可以回答：
- 封口機
- 冰箱
- 製冰機
- 飲料操作
- 現場服務
- 客人應對
- 基礎設備排除
- 清潔問題
- SOP 問題

但以下內容禁止回答：
1. 配方比例
2. 原物料成本
3. 毛利
4. 未公開加盟政策
5. 危險拆機與改電路

如果遇到設備問題：
請先提供：
1. 可能原因
2. 基本檢查方式
3. 現場先做什麼
4. 何時通知主管

如果真的不確定：
再請對方拍照或錄影聯繫總部。

員工問題：
{user_message}
"""
    response = model.generate_content(prompt)
    reply_to_line(event, response.text)


# =========================
# Render 啟動 Flask 伺服器
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
