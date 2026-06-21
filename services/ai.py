import json
import re

import google.generativeai as genai

import state as app_state
from config import model
from sheets.helpers import normalize_text


def classify_message(user_message):
    message = normalize_text(user_message)

    recipe_keywords = [
        "配方", "比例", "幾克", "幾公克", "多少克",
        "原物料", "成本", "毛利", "煮法", "熬煮",
        "仙草汁", "黑糖", "二砂", "冬瓜糖", "甘草",
        "海鹽", "茶包"
    ]

    food_quality_keywords = [
        "壞掉", "酸掉", "發酸", "怪味", "異味",
        "出水", "太軟", "變色", "發霉", "異物",
        "能不能賣", "可不可以賣",
        "茶湯混濁", "混濁", "沉澱", "結塊"
    ]

    food_quality_context_keywords = ["仙草凍", "茶湯", "糖水", "原料", "飲品", "仙草"]

    qa_keywords = [
        "封口機", "錯誤", "錯誤代碼", "卡膜", "封膜",
        "封不起來", "封不緊", "漏杯", "杯膜", "機器",
        "E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E09",
        "客訴", "太甜", "太淡", "太苦", "沒味道", "味道不對"
    ]

    if any(word in user_message for word in recipe_keywords):
        return "recipe"

    if any(word in user_message for word in food_quality_keywords):
        return "food_quality"

    if "正常嗎" in user_message and any(word in user_message for word in food_quality_context_keywords):
        return "food_quality"

    if "仙草凍正常嗎" in user_message:
        return "food_quality"

    if any(word in user_message for word in qa_keywords):
        return "qa"

    if re.search(r"E\d{2}", message):
        return "qa"

    return "general"

def find_recipe(user_message):
    records = app_state.recipe_cache

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
    user_msg_norm = normalize_text(user_message)

    for row in app_state.qa_cache:
        keywords = str(row.get("keywords", "")).split(",")
        answer = str(row.get("answer", "")).strip()
        permission = str(row.get("permission", "")).strip()

        if len(answer) < 20:
            continue

        matched = False

        for keyword in keywords:
            kw = normalize_text(keyword)

            if not kw:
                continue

            if re.fullmatch(r"E\d{2}", kw):
                if kw in user_msg_norm:
                    matched = True
                    break

            elif len(kw) >= 3 and kw in user_msg_norm:
                matched = True
                break

        if not matched:
            continue

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

def ask_gemini_text(user_message, msg_type):
    if msg_type == "food_quality":
        prompt = f"""你是三入好棧的資深店長，有十年飲料店現場經驗。

員工正在詢問食品或飲品品管問題，請用繁體中文直接回答。

回答風格：
- 像LINE訊息，口氣直接像資深店長
- 先講「現在馬上做什麼」
- 給具體判斷標準與處理步驟
- 安全優先：疑似變質、異味、發霉、異物，一律先暫停販售
- 說明什麼情況可以繼續賣、什麼情況要丟掉
- 控制在250字內，用數字或換行區隔步驟
- 不回答配方比例、成本、毛利

員工問題：{user_message}"""

    elif msg_type == "qa":
        prompt = f"""你是三入好棧的資深店長，有十年封口機與飲料店現場經驗。

員工正在詢問設備、客訴或現場SOP問題，請用繁體中文直接回答。

回答風格：
- 像LINE訊息，口氣直接像資深店長
- 先講「現在馬上做什麼」，再講原因
- 給具體動作步驟，照順序排列（最常見原因優先）
- 不要只說「重開機」或「聯絡廠商」，要給實際排除步驟
- 如果有高峰期應急替代方案，也要補充說明
- 控制在300字內，用數字或換行區隔步驟
- 禁止建議拆解馬達、電路板、高壓零件等電氣核心
- 不回答配方比例、成本、毛利

員工問題：{user_message}"""

    else:
        prompt = f"""你是三入好棧的資深店長。

員工有問題請教，請用繁體中文直接回答。

回答風格：
- 像資深店長教員工，口氣直接
- 給具體建議，不要廢話
- 控制在200字內
- 不回答配方比例、成本、毛利、未公開加盟資訊

員工問題：{user_message}"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "max_output_tokens": 2048,
            }
        )

        reply_text = response.text if response.text else ""

        if len(reply_text.strip()) < 20:
            reply_text = "我目前判斷不完整，請補充機器型號、錯誤畫面或現場狀況。"

    except Exception as e:
        print(f"[ERROR] ask_gemini_text 失敗: {e}")
        reply_text = "目前系統忙碌，請稍後再試或直接描述問題。"

    return reply_text

def parse_ai_json(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("AI 回傳空白內容")
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError(f"AI 未回傳 JSON，原始內容：{cleaned[:300]}")
    return json.loads(match.group(0))

def parse_ai_text_fallback(text, result_type):
    content = str(text or "").strip()
    if result_type == "cups":
        match = re.search(
            r'(?:"?cups"?\s*:|總杯數|杯數|共計|合計)'
            r"\D{0,12}([1-9]\d{0,5})",
            content
        )
        if match:
            return {"cups": int(match.group(1)), "confidence": 0.5, "note": "文字備援擷取"}

    if result_type == "expense":
        amount_match = re.search(
            r'(?:"?amount"?\s*:|總額|總計|應付|實付|金額|合計)\D{0,15}'
            r"(?:NT\$|NTD|\$)?\s*"
            r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)",
            content,
            flags=re.IGNORECASE
        )
        if amount_match:
            description_match = re.search(
                r'"description"\s*:\s*"([^"\r\n]{1,100})',
                content,
                flags=re.IGNORECASE
            )
            return {
                "description": (
                    description_match.group(1).strip()
                    if description_match
                    else "收據支出（待確認）"
                ),
                "amount": amount_match.group(1),
                "note": "不完整 JSON 備援擷取",
                "confidence": 0.5
            }

    raise ValueError(f"無法從 AI 文字回應擷取資料：{content[:300]}")

def analyze_image_as_json(image_path, prompt, result_type):
    image_file = genai.upload_file(image_path)
    response = model.generate_content(
        [prompt, image_file],
        generation_config={
            "temperature": 0,
            "max_output_tokens": 512,
            "response_mime_type": "application/json"
        }
    )
    raw_text = response.text if response.text else ""
    print("[AI] 圖片辨識原始回應:", raw_text)

    try:
        return parse_ai_json(raw_text)
    except Exception as first_error:
        print("[AI] 第一次 JSON 解析失敗:", first_error)

    retry_prompt = (
        prompt
        + "\n你上一次沒有依格式回答。請重新查看同一張圖片，"
        "這次只能輸出一個合法 JSON 物件，不要使用 Markdown、說明文字或程式碼區塊。"
    )
    retry_response = model.generate_content(
        [retry_prompt, image_file],
        generation_config={
            "temperature": 0,
            "max_output_tokens": 512
        }
    )
    retry_text = retry_response.text if retry_response.text else ""
    print("[AI] 圖片辨識重試回應:", retry_text)
    try:
        return parse_ai_json(retry_text)
    except Exception as second_error:
        print("[AI] 第二次 JSON 解析失敗:", second_error)
        fallback_errors = []
        for fallback_text in (retry_text, raw_text):
            if not fallback_text:
                continue
            try:
                return parse_ai_text_fallback(fallback_text, result_type)
            except Exception as fallback_error:
                fallback_errors.append(str(fallback_error))
        raise ValueError("；".join(fallback_errors) or "AI 回應無法擷取")
