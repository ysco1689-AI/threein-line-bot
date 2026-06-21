import state as app_state
from config import SPREADSHEET_ID
from sheets.client import get_google_client


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

    app_state.users_cache.append({
        "line_user_id": user_id,
        "name": "新使用者",
        "role": "guest",
        "status": "pending",
        "note": "自動加入"
    })

    print("已自動新增使用者:", user_id)


def is_known_user(user_id):
    return any(
        str(user.get("line_user_id", "")).strip() == user_id
        for user in app_state.users_cache
    )


def get_user_role(user_id):
    for user in app_state.users_cache:
        if str(user.get("line_user_id", "")).strip() == user_id:
            return {
                "role": str(user.get("role", "")).strip(),
                "status": str(user.get("status", "")).strip()
            }

    return {
        "role": "unknown",
        "status": "unknown"
    }
