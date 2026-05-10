from flask import Flask, request

from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)

from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = "qQp0pVy2Hv6PMRJt4AkzZ5mZwUr8B/l5V/O0PT0twWU8aUxVjyLdoD6ElwJ3FFt6GrrK2wgKXl/qbpY2vNshFFIUDrirB/u7CVL3KOxgpc1ssJcVLu8K6dP3k8VGvvD+MzF6cqJE6YknpELIlzdjUAdB04t89/1O/w1cDnyilFU="
CHANNEL_SECRET = "你的ChannelSecret"

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():

    signature = request.headers['X-Line-Signature']

    body = request.get_data(as_text=True)

    handler.handle(body, signature)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    user_message = event.message.text

    reply_text = f"三入好棧 AI收到：{user_message}"

    with ApiClient(configuration) as api_client:

        line_bot_api = MessagingApi(api_client)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=reply_text)
                ]
            )
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)