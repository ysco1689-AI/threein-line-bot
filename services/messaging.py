from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from config import configuration


def reply_to_line(event, reply_text, quick_reply=None):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            message = TextMessage(text=reply_text, quick_reply=quick_reply)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[message]
                )
            )
    except Exception as e:
        print(f"[ERROR] reply_to_line 失敗: {e}")

def push_message(user_id, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )
