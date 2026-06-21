import os

import google.generativeai as genai
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration

MATERIAL_ALIASES = {
    "仙草甘茶": ["仙甘", "仙草甘"],
    "大井紅茶": ["大紅", "大井紅", "井"],
    "青茶": ["青茶", "青"],
    "麥香紅茶": ["麥香", "麥香紅"],
    "冬瓜茶": ["冬瓜", "冬"],
    "糖液": ["糖液", "糖水"],
    "仙草凍": ["仙草凍", "仙凍"],
    "檸檬汁": ["檸檬", "檸檬汁"],
    "牛奶": ["牛奶", "奶"],
    "奶水": ["奶水"],
    "冰塊": ["冰塊", "冰"],
    "660紙杯": ["紙杯", "杯子", "660"],
    "杯蓋": ["杯蓋", "蓋子"],
    "大吸管": ["大吸管", "粗管"],
    "小吸管": ["小吸管", "細管"],
    "1杯袋": ["1杯袋", "單杯袋"],
    "2杯袋": ["2杯袋"],
    "4杯袋": ["4杯袋"],
    "封口膜": ["封口膜", "封膜"],
    "試飲杯": ["試飲杯", "試飲"],
}

MATERIAL_SETUP_LABELS = {
    "仙草甘茶": "仙甘（包）",
    "大井紅茶": "大紅（小包）",
    "青茶": "青茶（小包）",
    "麥香紅茶": "麥香紅（小包）",
    "冬瓜茶": "冬瓜（小包）",
    "糖液": "糖液（桶）",
    "仙草凍": "仙草凍（罐）",
    "檸檬汁": "檸檬汁（罐）",
    "牛奶": "牛奶（罐）",
    "奶水": "奶水（罐）",
    "660紙杯": "660紙杯（20條／箱）",
    "杯蓋": "杯蓋（個）",
    "大吸管": "大吸管（包）",
    "小吸管": "小吸管（包）",
    "1杯袋": "1杯袋（包）",
    "2杯袋": "2杯袋（包）",
    "4杯袋": "4杯袋（包）",
    "封口膜": "封口膜（捲）",
    "試飲杯": "試飲杯（40條／箱）",
    "冰塊": "冰塊（包）",
}

EVENT_TYPES = [
    "🥤 杯數問題",
    "📍 位置問題",
    "🏪 攤位問題",
    "👤 人員問題",
    "📦 原料問題",
    "⚡ 設備問題",
    "📋 其他狀況",
]

EVENT_HEADERS = [
    "日期",
    "LINE_ID",
    "姓名",
    "檔期名稱",
    "攤位編號",
    "事件類型",
    "事件說明",
    "照片連結",
    "處理狀態",
    "主管備註",
    "建立時間",
]

REMINDER_HEADERS = [
    "日期",
    "LINE_ID",
    "提醒類型",
    "發送時間",
    "發送結果",
    "錯誤訊息",
]

MATERIAL_COMPLETION_HEADERS = [
    "日期",
    "LINE_ID",
    "姓名",
    "檔期名稱",
    "攤位編號",
    "完成時間",
]

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_DRIVE_CLIENT_ID = os.getenv("GOOGLE_DRIVE_CLIENT_ID")
GOOGLE_DRIVE_CLIENT_SECRET = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET")
GOOGLE_DRIVE_REFRESH_TOKEN = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHIFT_SPREADSHEET_ID = os.getenv("SHIFT_SPREADSHEET_ID", SPREADSHEET_ID)
CUP_PHOTO_FOLDER_ID = os.getenv("CUP_PHOTO_FOLDER_ID")
EXPENSE_PHOTO_FOLDER_ID = os.getenv("EXPENSE_PHOTO_FOLDER_ID")
EVENT_PHOTO_FOLDER_ID = os.getenv("EVENT_PHOTO_FOLDER_ID")
REMINDER_API_KEY = os.getenv("REMINDER_API_KEY", os.getenv("ADMIN_SETUP_KEY", ""))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
