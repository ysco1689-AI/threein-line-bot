import os
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont


CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
RICH_MENU_IMAGE_PATH = Path(__file__).with_name("richmenu.png")


RICH_MENU_AREAS = [
    ("✅ 確認檔期", "action=confirm_shift", 0, 0),
    ("🥤 杯數回報", "action=report_cups", 400, 0),
    ("📦 餘料回報", "action=report_materials", 800, 0),
    ("💰 費用支出", "action=report_expense", 0, 405),
    ("🚗 里程回報", "action=report_mileage", 400, 405),
    ("📋 事件紀錄", "action=report_event", 800, 405),
]


def require_token():
    if not CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("請先設定 CHANNEL_ACCESS_TOKEN 環境變數")


def load_font(size):
    font_candidates = [
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msjhbd.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]

    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size)

    return ImageFont.load_default()


def draw_centered_text(draw, box, text, font, fill):
    left, top, right, bottom = box
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=8, align="center")
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = left + ((right - left) - text_width) / 2
    y = top + ((bottom - top) - text_height) / 2
    draw.multiline_text((x, y), text, font=font, fill=fill, spacing=8, align="center")


def create_richmenu_image():
    width, height = 1200, 810
    cell_width, cell_height = 400, 405
    image = Image.new("RGB", (width, height), "#f7faf7")
    draw = ImageDraw.Draw(image)
    title_font = load_font(42)
    small_font = load_font(24)

    colors = ["#edf7ed", "#fff8e5", "#edf4ff", "#fff0f0", "#effaf8", "#f5f1ff"]

    for index, (label, _data, x, y) in enumerate(RICH_MENU_AREAS):
        box = (x, y, x + cell_width, y + cell_height)
        draw.rectangle(box, fill=colors[index], outline="#263238", width=3)
        draw_centered_text(draw, box, label.replace(" ", "\n"), title_font, "#263238")

    draw.rectangle((0, 0, width - 1, height - 1), outline="#263238", width=5)
    draw.text((24, height - 42), "三入好棧 檔期回報系統", font=small_font, fill="#455a64")
    image.save(RICH_MENU_IMAGE_PATH, "PNG")
    return RICH_MENU_IMAGE_PATH


def create_rich_menu():
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "size": {"width": 1200, "height": 810},
        "selected": True,
        "name": "三入好棧檔期回報選單",
        "chatBarText": "檔期回報",
        "areas": [
            {
                "bounds": {"x": x, "y": y, "width": 400, "height": 405},
                "action": {"type": "postback", "data": data},
            }
            for _label, data, x, y in RICH_MENU_AREAS
        ],
    }
    response = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["richMenuId"]


def upload_rich_menu_image(rich_menu_id, image_path):
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "image/png",
    }
    with open(image_path, "rb") as image_file:
        response = requests.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            headers=headers,
            data=image_file,
            timeout=30,
        )
    response.raise_for_status()


def set_default_rich_menu(rich_menu_id):
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    response = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()


def main():
    require_token()
    image_path = create_richmenu_image()
    rich_menu_id = create_rich_menu()
    upload_rich_menu_image(rich_menu_id, image_path)
    set_default_rich_menu(rich_menu_id)
    print("Rich Menu 建立完成:", rich_menu_id)
    print("圖片檔案:", image_path)


if __name__ == "__main__":
    main()
