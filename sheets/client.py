import json

import gspread
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from config import (
    GOOGLE_DRIVE_CLIENT_ID,
    GOOGLE_DRIVE_CLIENT_SECRET,
    GOOGLE_DRIVE_REFRESH_TOKEN,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    SPREADSHEET_ID,
    SHIFT_SPREADSHEET_ID,
)


def get_google_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = ServiceAccountCredentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )
    return gspread.authorize(credentials)

def get_drive_credentials():
    missing = [
        name for name, value in [
            ("GOOGLE_DRIVE_CLIENT_ID", GOOGLE_DRIVE_CLIENT_ID),
            ("GOOGLE_DRIVE_CLIENT_SECRET", GOOGLE_DRIVE_CLIENT_SECRET),
            ("GOOGLE_DRIVE_REFRESH_TOKEN", GOOGLE_DRIVE_REFRESH_TOKEN)
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Google Drive OAuth 尚未設定：" + ", ".join(missing)
        )

    return UserCredentials(
        token=None,
        refresh_token=GOOGLE_DRIVE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_DRIVE_CLIENT_ID,
        client_secret=GOOGLE_DRIVE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive"]
    )

def get_shift_spreadsheet():
    gc = get_google_client()
    return gc.open_by_key(SHIFT_SPREADSHEET_ID)
