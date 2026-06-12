"""
IT Helper Agent — Slack Bolt + Azure OpenAI
Entry point. Run with: python agent.py
"""

import os
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

from conversation import handle_message  # used by Slack handlers and Outlook poller
from admin import handle_admin_command
from config import load_config
from permissions import is_admin, can_perform

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

ADMIN_CHANNEL = os.environ.get("ADMIN_CHANNEL_ID")


@app.event("app_mention")
def on_mention(event, say, client):
    """Agent responds when mentioned in any channel."""
    thread_ts = event.get("thread_ts") or event["ts"]
    user = event["user"]
    text = event["text"]
    channel = event["channel"]

    log.info(f"Mention from {user} in {channel}: {text}")

    clean_text = " ".join(
        word for word in text.split() if not word.startswith("<@")
    ).strip()

    response = handle_message(
        user_id=user,
        text=clean_text,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        is_admin=is_admin(user),
    )

    say(text=response, thread_ts=thread_ts)


@app.message()
def on_dm(message, say, client):
    """Agent responds to direct messages."""
    if message.get("channel_type") != "im":
        return

    user = message["user"]
    text = message.get("text", "")
    channel = message["channel"]
    thread_ts = message.get("thread_ts") or message["ts"]

    log.info(f"DM from {user}: {text}")

    # Admin training commands
    if text.startswith("!") and is_admin(user):
        response = handle_admin_command(text[1:].strip())
        say(text=response, thread_ts=thread_ts)
        return

    # Block admin commands from non-admins silently degrading to chat
    if text.startswith("!") and not is_admin(user):
        say(text="Sorry, that command isn't available.", thread_ts=thread_ts)
        return

    response = handle_message(
        user_id=user,
        text=text,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        is_admin=is_admin(user),
    )

    say(text=response, thread_ts=thread_ts)


@app.event("message")
def on_admin_channel(message, say):
    """Watch admin channel for training commands."""
    if message.get("channel") != ADMIN_CHANNEL:
        return
    if message.get("subtype"):
        return

    user = message["user"]
    text = message.get("text", "")

    if not is_admin(user):
        say("Only IT admins can post here.")
        return

    if text.startswith("!"):
        response = handle_admin_command(text[1:].strip())
        say(response)


if __name__ == "__main__":
    config = load_config()
    log.info(f"Starting IT Helper Agent | {len(config.get('rules', []))} rules loaded")

    # Email poller disabled — daily_summary.py handles email on a schedule
    # To re-enable: uncomment below and restart
    # if os.environ.get("ENTRA_TENANT_ID"):
    #     from outlook import start_poller
    #     start_poller(handle_message)
    #     log.info("Outlook poller started.")

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
