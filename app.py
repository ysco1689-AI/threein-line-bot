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
# 全域快取資料
# 避免每次訊息都重新讀 Google Sheet
# =========================

recipe_cache = []
users_cache = []
qa_cache = []


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
# 初始化所有 Google Sheet 資料
# 啟動時只讀一次
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
# users 表找不到時，預設新增為 guest / pending
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
# 根據 LINE user_id
# 查詢此人的角色與狀態
# 如果 users 表找不到，就自動新增成 guest / pending
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


# =========================
# 搜尋 qa 問答資料庫
# 根據 keywords 比對問題
# 並依 permission 判斷是否能回答
# =========================

def find_qa_answer(user_message, role, status):
    records = qa_cache

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
    signature = request.headers.get("X-Line-Signature")

    if not signature:
        return "Missing signature", 400

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
1. 夜市現場店長
2. 有經驗的飲料店員工
3. 回答親切自然
4. 不要太官方
5. 不要長篇大論

回答規則：
1. 優先解決當下問題
2. 最多列出1~3點
3. 每點盡量一句話
4. 不要超過 200 字
5. 不要像教科書
6. 現場人員要能5秒內看懂

如果問題很簡單：
直接講重點即可。

例如：
「E05通常是溫度沒上來，先重開機看看，再檢查封口膜有沒有卡住。如果還是一樣，再通知主管處理。」

不要過度解釋原理。
不要一次講太多種可能性。

你只能根據以下配方資料回答，不可以自行編造。

配方資料：
{recipe_text}

員工問題：
{user_message}
"""

        response = model.generate_content(prompt)

        reply_to_line(
            event,
            response.text if response.text else "目前無法產生回覆，請稍後再試。"
        )
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

    reply_to_line(
        event,
        response.text if response.text else "目前無法產生回覆，請稍後再試。"
    )


# =========================
# 啟動時先載入 Google Sheet
# =========================

load_all_data()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
