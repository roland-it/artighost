"""
IT Helper Agent — Slack Bolt + Azure OpenAI
Entry point. Run with: python agent.py
"""
import os
import base64
import logging
import requests
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

# Tracks thread_ts values the bot has replied in via @mention, so follow-up
# replies in the same thread (without re-mentioning) still reach the agent.
# In-memory only — resets on restart.
active_threads = set()


def _extract_images(files: list) -> list:
    """Download image files from a Slack message. Returns list of
    {mime_type, data} dicts ready for GPT vision input. Non-images skipped."""
    if not files:
        return []
    images = []
    token = os.environ["SLACK_BOT_TOKEN"]
    for f in files:
        mime = f.get("mimetype", "")
        if not mime.startswith("image/"):
            log.info(f"Skipping non-image file: {f.get('name')} ({mime})")
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            resp.raise_for_status()
            images.append({
                "mime_type": mime,
                "data": base64.b64encode(resp.content).decode("utf-8"),
            })
            log.info(f"Downloaded image: {f.get('name')} ({len(resp.content)} bytes)")
        except Exception as e:
            log.warning(f"Could not download Slack file {f.get('name')}: {e}")
    return images


@app.event("app_mention")
def on_mention(event, say, client):
    """Agent responds when mentioned in any channel."""
    thread_ts = event.get("thread_ts") or event["ts"]
    active_threads.add(thread_ts)
    user = event["user"]
    text = event["text"]
    channel = event["channel"]
    log.info(f"Mention from {user} in {channel}: {text}")
    clean_text = " ".join(
        word for word in text.split() if not word.startswith("<@")
    ).strip()
    images = _extract_images(event.get("files") or [])
    response = handle_message(
        user_id=user,
        text=clean_text,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        is_admin=is_admin(user),
        images=images,
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
    # DMs are flat, not threaded — only pass a real thread_ts if the user
    # actually replied in a thread (rare in DMs); otherwise None so
    # _build_messages pulls flat channel history instead.
    thread_ts = message.get("thread_ts")
    log.info(f"DM from {user}: {text}")
    # Admin training commands
    if text.startswith("!") and is_admin(user):
        response = handle_admin_command(text[1:].strip())
        say(text=response)
        return
    # Block admin commands from non-admins silently degrading to chat
    if text.startswith("!") and not is_admin(user):
        say(text="Sorry, that command isn't available.")
        return
    images = _extract_images(message.get("files") or [])
    response = handle_message(
        user_id=user,
        text=text,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        is_admin=is_admin(user),
        images=images,
    )
    say(text=response)


@app.event("message")
def on_thread_reply(message, say, client):
    """Agent responds to follow-up messages in a thread it already replied in
    via @mention — Slack's app_mention event only fires on the initial mention,
    not on subsequent replies in the same thread."""
    if message.get("channel_type") == "im":
        return  # on_dm already handles DMs, including DM threads
    if message.get("channel") == ADMIN_CHANNEL:
        return  # on_admin_channel already handles this
    if message.get("subtype"):
        return  # ignore edits, bot messages, joins, etc.

    thread_ts = message.get("thread_ts")
    if not thread_ts or thread_ts not in active_threads:
        return

    user = message["user"]
    text = message.get("text", "")
    channel = message["channel"]
    log.info(f"Thread reply from {user} in {channel}: {text}")

    images = _extract_images(message.get("files") or [])
    response = handle_message(
        user_id=user,
        text=text,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        is_admin=is_admin(user),
        images=images,
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
