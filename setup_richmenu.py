import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
RICH_MENU_IMAGE_PATH = Path(__file__).with_name("richmenu.png")


RICH_MENU_AREAS = [
    ("確認檔期", "action=confirm_shift", 0, 0),
    ("杯數回報", "action=report_cups", 400, 0),
    ("餘料回報", "action=report_materials", 800, 0),
    ("費用支出", "action=report_expense", 0, 405),
    ("里程回報", "action=report_mileage", 400, 405),
    ("事件紀錄", "action=report_event", 800, 405),
]


def require_token():
    if not CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("請先設定 CHANNEL_ACCESS_TOKEN 環境變數")


def load_font(size):
    font_candidates = [
        "C:/Windows/Fonts/msjhbd.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        str(Path(__file__).with_name("NotoSansTC-Bold.ttf")),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]

    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size)

    raise RuntimeError(
        "找不到可顯示繁體中文的字型。請在 Windows 本機執行 "
        "`python setup_richmenu.py --generate-only --regenerate`，"
        "再把產生的 richmenu.png 上傳到 GitHub。"
    )


def draw_centered_text(draw, box, text, font, fill):
    left, top, right, bottom = box
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=8, align="center")
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = left + ((right - left) - text_width) / 2
    y = top + ((bottom - top) - text_height) / 2
    draw.multiline_text((x, y), text, font=font, fill=fill, spacing=8, align="center")


def create_richmenu_image(force=False):
    if RICH_MENU_IMAGE_PATH.exists() and not force:
        return RICH_MENU_IMAGE_PATH

    width, height = 1200, 810
    cell_width, cell_height = 400, 405
    image = Image.new("RGB", (width, height), "#f7faf7")
    draw = ImageDraw.Draw(image)
    title_font = load_font(76)

    colors = ["#edf7ed", "#fff8e5", "#edf4ff", "#fff0f0", "#effaf8", "#f5f1ff"]

    for index, (label, _data, x, y) in enumerate(RICH_MENU_AREAS):
        box = (x, y, x + cell_width, y + cell_height)
        draw.rectangle(box, fill=colors[index], outline="#263238", width=3)
        draw_centered_text(draw, box, label, title_font, "#263238")

    draw.rectangle((0, 0, width - 1, height - 1), outline="#263238", width=5)
    image.save(RICH_MENU_IMAGE_PATH, "PNG")
    return RICH_MENU_IMAGE_PATH


def create_rich_menu():
    import requests

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
    import requests

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
    import requests

    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    response = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()


def link_rich_menu_to_user(rich_menu_id, user_id):
    import requests

    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    response = requests.post(
        f"https://api.line.me/v2/bot/user/{user_id}/richmenu/{rich_menu_id}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="只產生 richmenu.png，不呼叫 LINE API",
    )
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="強制重新產生 richmenu.png",
    )
    args = parser.parse_args()

    image_path = create_richmenu_image(force=args.regenerate)
    print("Rich Menu 圖片完成:", image_path)

    if args.generate_only:
        return

    require_token()
    rich_menu_id = create_rich_menu()
    upload_rich_menu_image(rich_menu_id, image_path)
    set_default_rich_menu(rich_menu_id)
    print("Rich Menu 建立完成:", rich_menu_id)


if __name__ == "__main__":
    main()
