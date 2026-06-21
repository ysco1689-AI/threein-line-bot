import os

from flask import Flask

import state
from config import SPREADSHEET_ID
from sheets.client import get_google_client

app = Flask(__name__)

recipe_cache = state.recipe_cache
users_cache = state.users_cache
qa_cache = state.qa_cache
user_states = state.user_states


def load_all_data():
    

    try:
        gc = get_google_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        try:
            state.recipe_cache = spreadsheet.sheet1.get_all_records()
        except Exception as e:
            state.recipe_cache = []
            print("配方資料載入失敗，暫時略過：", e)

        try:
            state.users_cache = spreadsheet.worksheet("users").get_all_records()
        except Exception as e:
            state.users_cache = []
            print("users 工作表載入失敗，暫時略過：", e)

        try:
            state.qa_cache = spreadsheet.worksheet("qa").get_all_records()
        except Exception as e:
            state.qa_cache = []
            print("qa 工作表載入失敗，暫時略過：", e)

        print("Google Sheet 資料載入成功")

    except Exception as e:
        print("讀取 Google Sheet 失敗：", e)


# Import routes after app is created so decorators can register handlers.
import routes.admin  # noqa: E402,F401
import routes.line_webhook  # noqa: E402,F401


load_all_data()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
