from datetime import datetime, timedelta, timezone
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from sheets.client import get_drive_credentials
from sheets.helpers import safe_filename_part


def upload_photo_to_drive(
    image_path,
    folder_id,
    category,
    user_name,
    message_id
):
    if not folder_id:
        raise RuntimeError(f"{category} 的 Google Drive 資料夾 ID 尚未設定")

    extension = Path(image_path).suffix.lower() or ".jpg"
    filename = (
        f"{datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')}_"
        f"{safe_filename_part(user_name)}_"
        f"{safe_filename_part(category)}_"
        f"{safe_filename_part(message_id)}{extension}"
    )
    credentials = get_drive_credentials()
    drive_service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )
    media = MediaFileUpload(
        image_path,
        mimetype="image/jpeg",
        resumable=False
    )
    uploaded = drive_service.files().create(
        body={
            "name": filename,
            "parents": [folder_id]
        },
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True
    ).execute()
    file_id = uploaded["id"]
    return {
        "id": file_id,
        "name": uploaded.get("name", filename),
        "url": uploaded.get(
            "webViewLink",
            f"https://drive.google.com/file/d/{file_id}/view"
        )
    }
