"""
Outlook poller.
Reads the agent's inbox via Microsoft Graph, processes new emails,
and feeds them into the agent core.

Runs as a background thread alongside agent.py.
Polls on a configurable interval (default 60s).
Marks emails as read after processing to avoid reprocessing.

Security:
- All mailbox access goes through safe_mailbox() — hard-locked to AGENT_EMAIL
- Email bodies are sanitized for prompt injection before hitting the LLM
"""

import os
import re
import time
import base64
import logging
import threading
import requests
from bs4 import BeautifulSoup

from permissions import safe_mailbox

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
POLL_INTERVAL = int(os.environ.get("OUTLOOK_POLL_INTERVAL", 60))


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|previous\s+|prior\s+)?(instructions?|prompts?|rules?|directives?)",
    r"you\s+are\s+now",
    r"new\s+instructions?\s*:",
    r"system\s+prompt",
    r"forget\s+(everything|all)",
    r"act\s+as\s+",
    r"jailbreak",
    r"disregard\s+(all\s+|previous\s+)?(instructions?|rules?)",
    r"pretend\s+(you\s+are|to\s+be)",
    r"your\s+new\s+role",
]

_INJECTION_RE = re.compile(
    "|".join(f"(?:{p})" for p in _INJECTION_PATTERNS),
    flags=re.IGNORECASE,
)


def sanitize(text: str) -> str:
    sanitized = _INJECTION_RE.sub("[removed]", text)
    if sanitized != text:
        log.warning("Prompt injection pattern detected and removed.")
    return sanitized.strip()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_token_cache: dict = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    tenant_id = os.environ["ENTRA_TENANT_ID"]
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    resp = requests.post(url, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["ENTRA_CLIENT_ID"],
        "client_secret": os.environ["ENTRA_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default",
    })
    resp.raise_for_status()
    data = resp.json()

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data["expires_in"]
    log.info("Graph token refreshed.")
    return _token_cache["token"]


def graph_get(path: str, params: dict = None, raw: bool = False):
    token = get_access_token()
    resp = requests.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    resp.raise_for_status()
    return resp if raw else resp.json()


def graph_patch(path: str, body: dict) -> None:
    token = get_access_token()
    resp = requests.patch(
        f"{GRAPH_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

def extract_inline_images(email: dict, message_id: str) -> list[dict]:
    """
    Extract inline images from email body as base64.
    Returns list of {data: base64_string, mime_type: str}
    """
    images = []
    mailbox = safe_mailbox(os.environ["AGENT_EMAIL"])

    # Get full HTML body
    body_content = email.get("body", {}).get("content", "")
    body_type = email.get("body", {}).get("contentType", "text")

    if body_type.lower() != "html" or not body_content:
        return images

    # Parse inline image references (cid: references)
    soup = BeautifulSoup(body_content, "html.parser")
    cid_refs = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("cid:"):
            cid_refs.add(src[4:])  # strip "cid:"

    if not cid_refs:
        log.info(f"No inline image references found in email body.")
        return images

    log.info(f"Found {len(cid_refs)} cid references: {cid_refs}")

    # Fetch attachments and match by contentId
    try:
        attachments = graph_get(
            f"/users/{mailbox}/messages/{message_id}/attachments",
        )
        for att in attachments.get("value", []):
            if not att.get("isInline"):
                continue
            content_id = att.get("contentId", "").strip("<>")
            if content_id in cid_refs or att.get("name") in cid_refs:
                full_att = graph_get(
                    f"/users/{mailbox}/messages/{message_id}/attachments/{att['id']}"
                )
                content_bytes = full_att.get("contentBytes")
                if content_bytes:
                    images.append({
                        "data": content_bytes,
                        "mime_type": att.get("contentType", "image/png"),
                    })
                    log.info(f"Extracted inline image: {att.get('name')} ({att.get('contentType')})")
    except Exception as e:
        log.warning(f"Could not fetch attachments for {message_id}: {e}")

    return images


# ---------------------------------------------------------------------------
# Email fetching
# ---------------------------------------------------------------------------

def fetch_unread_emails() -> list:
    mailbox = safe_mailbox(os.environ["AGENT_EMAIL"])
    path = f"/users/{mailbox}/mailFolders/inbox/messages"

    data = graph_get(path, params={
        "$filter": "isRead eq false",
        "$orderby": "receivedDateTime asc",
        "$top": 25,
        "$select": "id,subject,from,receivedDateTime,body,bodyPreview",
    })

    return data.get("value", [])


def mark_as_read(message_id: str) -> None:
    mailbox = safe_mailbox(os.environ["AGENT_EMAIL"])
    graph_patch(
        f"/users/{mailbox}/messages/{message_id}",
        {"isRead": True},
    )


def send_reply(message_id: str, reply_body: str) -> None:
    mailbox = safe_mailbox(os.environ["AGENT_EMAIL"])
    token = get_access_token()
    resp = requests.post(
        f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}/reply",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"message": {}, "comment": reply_body},
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_email(email: dict, handler) -> None:
    subject = email.get("subject", "(no subject)")
    sender = email.get("from", {}).get("emailAddress", {})
    sender_email = sender.get("address", "unknown")
    sender_name = sender.get("name", sender_email)
    body_preview = sanitize(email.get("bodyPreview", ""))
    message_id = email["id"]
    received = email.get("receivedDateTime", "")

    log.info(f"Processing email from {sender_email}: {subject}")

    # Extract inline images
    images = extract_inline_images(email, message_id)
    if images:
        log.info(f"Found {len(images)} inline image(s) in email.")

    prompt = (
        f"You received an email.\n"
        f"From: {sender_name} <{sender_email}>\n"
        f"Subject: {sanitize(subject)}\n"
        f"Received: {received}\n\n"
        f"Message:\n{body_preview}"
    )

    if images:
        prompt += f"\n\nThis email contains {len(images)} inline image(s) which are included for your analysis."

    try:
        response = handler(
            user_id=sender_email,
            text=prompt,
            channel="email",
            thread_ts=message_id,
            client=None,
            is_admin=False,
            images=images,  # pass images to handler
        )
        log.info(f"Agent response for {message_id}: {response[:100]}...")
        mark_as_read(message_id)
        log.info(f"[EMAIL HANDLED] From: {sender_email} | Subject: {subject}\nResponse: {response}")

    except Exception as e:
        log.error(f"Failed to process email {message_id}: {e}")


# ---------------------------------------------------------------------------
# Poller loop
# ---------------------------------------------------------------------------

def poll_loop(handler) -> None:
    log.info(f"Outlook poller started. Checking every {POLL_INTERVAL}s.")
    while True:
        try:
            emails = fetch_unread_emails()
            if emails:
                log.info(f"Found {len(emails)} unread email(s).")
                for email in emails:
                    process_email(email, handler)
            else:
                log.debug("No unread emails.")
        except Exception as e:
            log.error(f"Poller error: {e}")

        time.sleep(POLL_INTERVAL)


def start_poller(handler) -> threading.Thread:
    t = threading.Thread(target=poll_loop, args=(handler,), daemon=True)
    t.start()
    return t
