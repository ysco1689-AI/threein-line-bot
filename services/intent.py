import re

EXPENSE_KEYWORDS = ["支出", "花費", "花了", "買了", "付了", "元", "塊錢", "$"]
CUP_KEYWORDS = ["杯", "賣了", "賣出", "銷售", "杯數"]


def detect_report_intent(message, material_aliases):
    has_number = bool(re.search(r"\d+", str(message or "")))
    if not has_number:
        return (None, None)

    if any(keyword in message for keyword in EXPENSE_KEYWORDS):
        return ("expense", None)

    if any(keyword in message for keyword in CUP_KEYWORDS):
        return ("cup", None)

    for full_name, aliases in material_aliases.items():
        all_names = [full_name] + aliases
        for name in all_names:
            if name and name in message:
                return ("material", full_name)

    return (None, None)
