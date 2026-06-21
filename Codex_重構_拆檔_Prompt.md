# Codex 重構 Prompt：app.py 拆分為套件結構

## 任務總覽

把現有的單一檔案 `app.py`（4450 行）拆分成多個職責分離的模組，方便日後除錯時能準確定位問題所在的檔案。**這是重構任務，不是新增功能**——拆分後的程式邏輯必須與現有完全相同，只是分散到不同檔案。

部署方式不變（Render 仍然執行 `app.py` 作為入口），只是把內部程式碼改成 import 多個模組。

---

## 第一步：建立新的資料夾結構

在 repo 根目錄建立以下結構：

```
threein-line-bot/
├── app.py                      # 精簡後的入口檔案
├── config.py                   # 環境變數與常數
├── requirements.txt            # 不變
├── Procfile                    # 不變
├── scheduler.py                # 不變
├── setup_richmenu.py           # 不變
├── richmenu.png                # 不變
├── routes/
│   ├── __init__.py
│   ├── line_webhook.py         # callback／postback／message handler 入口
│   └── admin.py                 # /run-reminders／/setup-richmenu／/richmenu-preview
├── handlers/
│   ├── __init__.py
│   ├── router.py                # handle_active_flow、材料清單偵測、分流邏輯
│   ├── shift.py                  # 確認檔期流程
│   ├── cup.py                    # 杯數回報流程
│   ├── mileage.py                # 里程回報流程
│   ├── expense.py                # 費用支出流程
│   ├── material.py               # 餘料回報流程
│   └── event.py                  # 事件紀錄流程
├── services/
│   ├── __init__.py
│   ├── users.py                  # 身份白名單查詢與守門
│   ├── ai.py                     # Gemini 文字／Vision 辨識呼叫
│   ├── photo.py                  # Google Drive 照片上傳
│   └── intent.py                 # （新增，見下方說明）全局自然語言回報意圖偵測
└── sheets/
    ├── __init__.py
    ├── client.py                  # Google Sheets 連線與認證
    └── helpers.py                 # 日期格式化、表頭讀取等共用函式
```

每個資料夾都要有空的 `__init__.py`，讓 Python 把它們視為套件。

---

## 第二步：函式搬移對照表

依照原始 `app.py` 的行號區段，把函式搬到對應的新檔案。**搬移時保持函式內容完全不變**，只調整必要的 import。

### config.py
搬移內容：第 1-153 行的所有環境變數讀取（`CHANNEL_ACCESS_TOKEN` 到 `REMINDER_API_KEY`）、`MATERIAL_ALIASES`、`MATERIAL_SETUP_LABELS`、`EVENT_TYPES`、`EVENT_HEADERS`、`REMINDER_HEADERS`、`MATERIAL_COMPLETION_HEADERS`、Gemini 的 `genai.configure()` 與 `model = genai.GenerativeModel(...)`、LINE 的 `configuration` 與 `handler` 物件初始化。

### sheets/client.py
搬移函式：`get_google_client()`、`get_drive_credentials()`、`get_spreadsheet()`、`get_shift_spreadsheet()`

**注意**：`get_spreadsheet()` 經掃描確認從未被呼叫（死碼），**請刪除這個函式，不要搬移**。

### sheets/helpers.py
搬移函式：`safe_filename_part()`、`normalize_date_text()`、`parse_date()`、`today_text()`、`now_time_text()`、`get_records_by_header()`、`normalize_text()`

### services/users.py
搬移函式：`add_new_user()`、`get_user_role()`

**這裡需要修改邏輯（不是單純搬移）**，請見下方「第三步：身份守門邏輯修改」。

### services/photo.py
搬移函式：`upload_photo_to_drive()`

### services/ai.py
搬移函式：`ask_gemini_text()`、`find_recipe()`、`find_qa_answer()`、`classify_message()`、`parse_ai_json()`、`parse_ai_text_fallback()`、`analyze_image_as_json()`

這些函式依賴 `recipe_cache`、`qa_cache` 全域變數，搬移時改為從 `app.py` import，或改寫成參數傳入（建議改參數傳入，更乾淨）。

### services/intent.py（新建檔案，內容是全新邏輯）
這個檔案目前不存在，需要新增。內容見下方「第四步：新增全局意圖偵測」。

### handlers/shift.py
搬移函式：`find_accessible_shift()`、`find_latest_started_shift()`、`check_shift_deadline()`、`require_report_access()`、`find_today_shift()`、`write_shift_confirmation()`、`confirm_quick_reply()`、`start_confirm_shift_flow()`、`mark_shift_confirmed()`、`check_shift_confirmed()`、`require_shift_confirmed()`、`get_confirmed_shift()`、`handle_confirm_shift_text()`

### handlers/cup.py
搬移函式：`date_quick_reply()`、`cup_update_quick_reply()`、`input_method_quick_reply()`、`ai_result_quick_reply()`、`available_cup_dates()`、`start_cup_report_flow()`、`find_cup_record()`、`write_new_cup_record()`、`update_cup_record()`、`finish_cup_flow()`、`submit_cup_value()`、`handle_cup_report_text()`、`handle_cup_photo_result()`

### handlers/mileage.py
搬移函式：`drive_quick_reply()`、`mileage_manage_quick_reply()`、`mileage_plate_quick_reply()`、`find_today_mileage_records()`、`write_mileage_record()`、`start_mileage_report_flow()`、`finish_mileage_flow()`、`parse_mileage_number()`、`normalize_plate_last_four()`、`update_mileage_plate()`、`update_mileage_record()`、`format_mileage_number()`、`handle_mileage_report_text()`

### handlers/expense.py
搬移函式：`payment_method_quick_reply()`、`expense_duplicate_quick_reply()`、`expense_submit_quick_reply()`、`expense_continue_quick_reply()`、`start_expense_report_flow()`、`reset_expense_entry()`、`finish_expense_flow()`、`parse_expense_amount()`、`format_expense_amount()`、`find_duplicate_expenses()`、`write_expense_record()`、`show_expense_summary()`、`save_expense_and_continue()`、`handle_expense_report_text()`、`handle_expense_photo_result()`

### handlers/material.py
搬移函式：`material_confirm_quick_reply()`、`material_continue_quick_reply()`、`material_setup_quick_reply()`、`parse_material_quantity()`、`parse_material_transaction_quantity()`、`load_material_settings()`、`find_material_setting()`、`extract_material_quantity()`、`build_material_initial_template()`、`parse_material_initial_batch()`、`parse_material_transaction_batch()`、`get_shift_material_initial_records()`、`get_shift_material_initials()`、`save_shift_material_initial()`、`save_shift_material_initial_batch()`、`get_material_records()`、`material_records_for_shift()`、`calculate_material_used()`、`material_report_date()`、`is_material_final_day()`、`write_material_record()`、`write_material_records_batch()`、`recompute_material_balances()`、`recompute_material_balances_batch()`、`show_material_total()`、`show_material_initials()`、`start_material_report_flow()`、`finish_material_flow()`、`write_material_completion()`、`handle_material_report_text()`

**注意**：`find_latest_material_record()` 與 `update_material_record()` 經掃描確認從未被呼叫（已被 `recompute_material_balances_batch()` 取代的舊版死碼），**請刪除這兩個函式，不要搬移**。

### handlers/event.py
搬移函式：`event_action_quick_reply()`、`event_type_quick_reply()`、`event_photo_quick_reply()`、`event_filter_quick_reply()`、`get_user_name()`、`get_event_sheet()`、`get_event_context()`、`write_event_record()`、`get_event_records()`、`finish_event_flow()`、`start_event_flow()`、`submit_event_record()`、`show_event_history()`、`handle_event_report_text()`、`handle_event_photo_result()`

### handlers/router.py
搬移函式：`handle_active_flow()`、`handle_material_template_recovery()`

**這裡需要修改邏輯（不是單純搬移）**，請見下方「第五步：插入全局意圖偵測呼叫點」。

### routes/admin.py
搬移函式：`get_reminder_sheet()`、`get_today_shift_rows()`、`get_today_confirmed_ids()`、`get_today_material_reported_ids()`、`get_sent_reminder_keys()`、`log_reminder()`、`send_reminders()`、`run_scheduled_reminders()`

搬移 Flask 路由：`run_reminders_route()`（含 `@app.route("/run-reminders", ...)`）、`setup_richmenu_route()`（含 `@app.route("/setup-richmenu", ...)`）、`richmenu_preview_route()`（含 `@app.route("/richmenu-preview", ...)`）

### routes/line_webhook.py
搬移函式與路由：`callback()`（含 `@app.route("/callback", ...)`）、`handle_image_message()`（含 `@handler.add(MessageEvent, message=ImageMessageContent)`）、`handle_video_message()`（含 `@handler.add(MessageEvent, message=VideoMessageContent)`）、`handle_postback()`（含 `@handler.add(PostbackEvent)`）、`handle_message()`（含 `@handler.add(MessageEvent, message=TextMessageContent)`）

`reply_to_line()`、`push_message()` 這兩個共用函式也放在這裡，因為它們是 LINE 訊息收發的核心工具，跟 webhook 處理邏輯密切相關。

### app.py（精簡後的新內容）
只保留：
- `recipe_cache`、`users_cache`、`qa_cache`、`user_states` 這四個全域狀態變數的宣告
- `load_all_data()` 函式（注意它會修改 `recipe_cache`、`users_cache`、`qa_cache`，搬移時要確認 global 宣告正確）
- Flask app 建立：`app = Flask(__name__)`
- 從 `routes/`、`handlers/` 等模組 import 並註冊路由
- 檔案最底部的 `load_all_data()` 呼叫與 `if __name__ == "__main__":` 區塊

---

## 第三步：身份守門邏輯修改（services/users.py）

現有的 `get_user_role()` 邏輯是查無此人時自動新增使用者並繼續回應。修改為查無此人時不回應。

**現有邏輯**（供參考，請勿照抄，這是要被取代的版本）：
```python
def get_user_role(user_id):
    for user in users_cache:
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
```

**修改方向**：
1. 新增一個函式 `is_known_user(user_id)`，回傳 True/False，只查 `users_cache`，不寫入任何資料。
2. `get_user_role()` 改為查無此人時回傳一個明確的標記（例如 `{"role": "unknown", "status": "unknown"}`），**不要呼叫 `add_new_user()`**。
3. 在 `routes/line_webhook.py` 的 `handle_message()` 與 `handle_postback()` 開頭，取得 `user_info` 後立即檢查：如果 `role == "unknown"`，直接 `return`，不呼叫 `reply_to_line()`，不執行任何後續邏輯。
4. `add_new_user()` 函式保留（可能總部未來想用其他方式手動新增白名單成員時還會用到），但**不要在訊息處理流程中自動呼叫它**。

---

## 第四步：新增全局意圖偵測（services/intent.py，全新檔案）

這是新功能，用來支援「不按按鍵、直接打自然語言句子」也能觸發回報流程，例如團主直接輸入「冰塊500元」「580杯」「仙甘4包」。

### 設計邏輯

用關鍵字規則先粗篩，不呼叫 AI，把明確的句子直接導向對應流程；規則判斷不出來的句子，不處理，讓它繼續往下走原本的 `classify_message()` 邏輯（聊天/QA/配方查詢）。

```python
import re

EXPENSE_KEYWORDS = ["支出", "花費", "花了", "買了", "付了", "元", "塊錢", "$"]
CUP_KEYWORDS = ["杯", "賣了", "賣出", "銷售", "杯數"]

def detect_report_intent(message, material_aliases):
    """
    回傳值：
        ("expense", None) - 判定為費用支出
        ("cup", None) - 判定為杯數回報
        ("material", matched_name) - 判定為原物料回報，matched_name 為對照到的正式品項名稱
        (None, None) - 規則判斷不出來，交給後續邏輯處理
    """
    has_number = bool(re.search(r'\d+', message))

    if not has_number:
        return (None, None)

    # 規則：原物料判斷（品項簡稱/全名 + 數字）
    for full_name, aliases in material_aliases.items():
        all_names = [full_name] + aliases
        for name in all_names:
            if name in message:
                return ("material", full_name)

    # 規則：費用支出判斷（費用字眼 + 數字）
    if any(keyword in message for keyword in EXPENSE_KEYWORDS):
        return ("expense", None)

    # 規則：杯數判斷（杯數字眼 + 數字）
    if any(keyword in message for keyword in CUP_KEYWORDS):
        return ("cup", None)

    return (None, None)
```

**請注意**：原物料判斷要排在費用判斷前面，因為「冰塊500元」這句話裡，「冰塊」會比對到 `MATERIAL_ALIASES`，但使用情境其實是費用支出（買冰塊花了500元）而不是回報原物料剩餘量。

**這裡有歧義需要規則優先順序處理**，建議規則改為：
1. 先檢查是否同時符合「費用字眼」+ 數字 → 判定 expense（費用判斷優先權最高，因為「元」「花了」是非常明確的訊號）
2. 再檢查是否符合「杯數字眼」+ 數字 → 判定 cup
3. 最後才檢查「品項名稱」+ 數字 → 判定 material（因為品項名稱在費用句子裡也可能出現，例如「冰塊500元」，要避免誤判）

請按此優先順序重新排列上面範例程式碼的判斷順序。

---

## 第五步：插入全局意圖偵測呼叫點（handlers/router.py）

在 `handle_active_flow()` 已經判斷「沒有進行中流程」、且 `handle_material_template_recovery()` 也判斷「不是材料清單格式」之後、原本流向 `classify_message()` 之前，插入意圖偵測呼叫。

修改 `routes/line_webhook.py` 的 `handle_message()`，加入以下邏輯（插入位置：在 `handle_active_flow` 與 `handle_material_template_recovery` 之後，`classify_message` 之前）：

```python
from services.intent import detect_report_intent
from config import MATERIAL_ALIASES

# ... 在 handle_active_flow 和 handle_material_template_recovery 都回傳 False 之後

intent_type, matched_material = detect_report_intent(user_message, MATERIAL_ALIASES)

if intent_type == "expense":
    if not require_report_access(event, user_id):
        return
    start_expense_report_flow(event, user_id)
    # 並將 user_message 暫存到 state 裡，讓 expense 流程可以直接帶入這句話的內容
    # 而不是又重新問一次，具體做法：呼叫 start_expense_report_flow 後，
    # 立即把 user_message 傳給 handle_expense_report_text 處理一次，
    # 讓系統嘗試從這句話解析出金額與說明
    return

if intent_type == "cup":
    if not require_report_access(event, user_id):
        return
    start_cup_report_flow(event, user_id)
    return

if intent_type == "material":
    if not require_report_access(event, user_id):
        return
    # 餘料已有自然語言解析能力（parse_material_transaction_batch 等），
    # 直接導入 report_materials 流程狀態後，把這句話交給
    # handle_material_report_text 處理，讓既有的解析邏輯接手
    ...
    return

# 都沒命中，才繼續走原本的 classify_message 邏輯
```

**重要提醒**：上面這段是邏輯示意，不是可以直接貼上的完整程式碼。請你（Codex）根據 `start_expense_report_flow`、`start_cup_report_flow`、`start_material_report_flow` 這幾個既有函式實際開啟的 state 結構（`flow`、`step`、`data` 等欄位），設計正確的銜接方式，讓「先觸發流程，再把這句話的內容立即丟給對應的 `handle_xxx_report_text` 處理一次」這件事能夠運作，使用者不需要再重複輸入一次。如果某個流程（例如費用支出，需要先選日期）沒辦法一句話跳過所有步驟，至少要做到：自動帶入「今天」這個日期，跳過選日期這一步，直接進入下一個缺漏的欄位詢問。

---

## 第六步：所有檔案的 import 修正

拆分後，各模組之間會互相依賴（例如 `handlers/cup.py` 需要 `sheets/client.py` 的 `get_shift_spreadsheet`，需要 `sheets/helpers.py` 的 `today_text`、`now_time_text`，需要 `routes/line_webhook.py` 的 `reply_to_line`）。

請你（Codex）在搬移每個函式後，**逐一檢查函式內部用到了哪些其他函式或變數**，並在檔案開頭加上正確的 `from xxx import yyy`。

特別注意全域變數的處理：
- `user_states`：建議定義在 `app.py`，其他模組用 `from app import user_states` 引入。由於 Python 字典是可變物件，其他模組對它做 `.get()`、`[...] = ...`、`.pop()` 等操作，會直接反映到原本的字典上，這樣可以避免循環 import 問題。
- `recipe_cache`、`users_cache`、`qa_cache`：同樣定義在 `app.py`，`load_all_data()` 函式也留在 `app.py`（因為它會用 `global` 修改這三個變數），`services/users.py` 與 `services/ai.py` 需要讀取這些快取時，用 `from app import users_cache` 等方式引入。

如果遇到循環 import（例如 `app.py` 要 import `routes/line_webhook.py`，但 `routes/line_webhook.py` 又要 import `app.py` 的 `user_states`），請用以下方式解決：把 `user_states`、`recipe_cache`、`users_cache`、`qa_cache` 這四個全域變數移到一個新檔案 `state.py`（放在根目錄），所有模組都從 `state.py` import，`app.py` 也從 `state.py` import，避免互相依賴。

---

## 第七步：完成後的測試清單

請你（Codex）完成重構後，自行確認以下事項：

- [ ] `python -c "import app"` 可以成功執行，沒有 import 錯誤
- [ ] 所有原本存在的函式都能在新位置找到（除了已確認刪除的 3 個死碼函式：`get_spreadsheet`、`find_latest_material_record`、`update_material_record`）
- [ ] `app.py` 檔案行數大幅減少（目標 100 行以內）
- [ ] 每個 `handlers/*.py` 檔案只包含對應功能的程式碼，不要混雜其他功能
- [ ] `git diff` 確認沒有任何函式的程式邏輯被意外修改（除了第三步、第四步、第五步明確要求修改的部分）
- [ ] 新增的 `services/intent.py` 邏輯與既有程式碼風格一致（變數命名、註解風格）

完成後，請列出一份「函式搬移總表」，格式為：原函式名稱 → 新檔案路徑，讓使用者可以對照確認。

---

## 重要原則提醒

1. **這是重構，不是重寫**：除了第三、四、五步明確要求的邏輯修改，其他所有函式內容必須與原始 `app.py` 逐字相同，只是換了檔案位置。
2. **不要自行「順手優化」其他程式碼**：例如看到某段邏輯寫得不夠精簡，不要因為重構任務就順便改寫，保持原樣搬移，只在指定的三個地方做指定的修改。
3. **保留所有中文註解與訊息文字**：使用者是非工程背景，LINE 回覆訊息的繁體中文內容一個字都不要改。
4. **Render 部署設定不變**：`Procfile` 內容不需要修改，因為 `app.py` 仍然是 Flask app 的入口檔案。
