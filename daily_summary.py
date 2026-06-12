"""
Daily System Status Summary
Reads unread emails from the Artighost inbox (12AM-8AM Eastern window),
analyzes each with vision, generates a consolidated system status summary,
posts to Slack, and marks all emails as read.

Usage:
  python daily_summary.py          # runs normally (12AM-8AM Eastern today)
  python daily_summary.py --now    # bypasses time check, uses last 8 hours
"""

import os
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

import requests
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SUMMARY_CHANNEL = os.environ.get("SUMMARY_CHANNEL_ID", "UGURMDDJ8")
EASTERN = ZoneInfo("America/New_York")

openai_client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_token_cache: dict = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    import time
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
    return _token_cache["token"]


def graph_get(path: str, params: dict = None) -> dict:
    token = get_access_token()
    resp = requests.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    resp.raise_for_status()
    return resp.json()


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
# Time window
# ---------------------------------------------------------------------------

def get_time_window(test_mode: bool) -> tuple[datetime, datetime]:
    if test_mode:
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=8)
    else:
        now_eastern = datetime.now(EASTERN)
        today = now_eastern.date()
        since = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=EASTERN).astimezone(timezone.utc)
        until = datetime(today.year, today.month, today.day, 8, 0, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    return since, until


# ---------------------------------------------------------------------------
# Email + image fetching
# ---------------------------------------------------------------------------

def fetch_emails_in_window(since: datetime, until: datetime) -> list:
    mailbox = os.environ["AGENT_EMAIL"]
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info(f"Fetching emails from {since_str} to {until_str} (UTC)")

    data = graph_get(
        f"/users/{mailbox}/mailFolders/inbox/messages",
        params={
            "$filter": (
                f"isRead eq false "
                f"and receivedDateTime ge {since_str} "
                f"and receivedDateTime le {until_str}"
            ),
            "$orderby": "receivedDateTime asc",
            "$top": 50,
            "$select": "id,subject,from,receivedDateTime,body,bodyPreview",
        },
    )

    emails = data.get("value", [])
    log.info(f"Found {len(emails)} unread email(s) in window.")
    return emails


def extract_inline_images(email: dict) -> list[dict]:
    """Extract inline images from email HTML body. Returns list of {data, mime_type}."""
    images = []
    mailbox = os.environ["AGENT_EMAIL"]
    message_id = email["id"]

    body_content = email.get("body", {}).get("content", "")
    body_type = email.get("body", {}).get("contentType", "text")

    if body_type.lower() != "html" or not body_content:
        return images

    soup = BeautifulSoup(body_content, "html.parser")
    cid_refs = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("cid:"):
            cid_refs.add(src[4:])

    if not cid_refs:
        return images

    log.info(f"Found {len(cid_refs)} cid reference(s) in email: {email.get('subject', '')}")

    try:
        attachments = graph_get(
            f"/users/{mailbox}/messages/{message_id}/attachments",
            params={"$select": "id,name,contentType,contentId,isInline"},
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
                    log.info(f"Extracted image: {att.get('name')}")
    except Exception as e:
        log.warning(f"Could not extract images from {message_id}: {e}")

    return images


def mark_as_read(message_id: str) -> None:
    mailbox = os.environ["AGENT_EMAIL"]
    graph_patch(f"/users/{mailbox}/messages/{message_id}", {"isRead": True})


# ---------------------------------------------------------------------------
# Per-email vision analysis
# ---------------------------------------------------------------------------

def build_email_analysis_prompt() -> str:
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo("America/New_York")
    from datetime import datetime
    today = datetime.now(eastern).date()
    yesterday = today - timedelta(days=1)
    today_str = today.strftime("%A, %B %d, %Y")
    yesterday_str = yesterday.strftime("%A, %B %d, %Y")

    return f"""
You are analyzing a monitoring email received by the IT team at Roland Foods.

Today is {today_str}. Yesterday was {yesterday_str}.

Roland Foods receives these types of monitoring emails:
- Financial/operational reports with numerical data in images (totals, comparisons). Flag any discrepancy over 1,000 between figures that should match. Also flag if yesterday's date ({yesterday_str}) does not appear in a list of dates in the image, unless yesterday was a Saturday or Sunday.
- Order mismatch reports showing tabular data of orders needing correction. State exact count or confirm zero.
- Process/job failure alerts. Summarize what failed and any available context.
- General system status updates.
- Spam or non-IT-relevant content.

Analyze the email including any images. Respond with JSON only:
{{
  "classification": "financial_report|order_mismatch|process_failure|system_status|ignore",
  "status": "ok|warning|error|ignore",
  "summary": "one or two sentence factual summary with specific numbers from images where available",
  "action_required": true/false,
  "action_note": "what needs to be done, or null"
}}

Be specific. Use actual numbers you can see in images. Do not be vague.
If the email is clearly spam or non-IT-relevant, classify as ignore.
""".strip()


def analyze_email(email: dict, images: list) -> dict:
    """Analyze a single email with vision. Returns analysis dict."""
    sender = email.get("from", {}).get("emailAddress", {})
    subject = email.get("subject", "(no subject)")
    preview = email.get("bodyPreview", "")[:500]

    text = (
        f"From: {sender.get('address', 'unknown')}\n"
        f"Subject: {subject}\n"
        f"Preview: {preview}"
    )

    # Build content with images if available
    if images:
        content = [{"type": "text", "text": text}]
        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img['mime_type']};base64,{img['data']}",
                    "detail": "high",
                },
            })
    else:
        content = text

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-1",
            messages=[
                {"role": "system", "content": build_email_analysis_prompt()},
                {"role": "user", "content": content},
            ],
            max_tokens=400,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        result["subject"] = subject
        result["from"] = sender.get("address", "unknown")
        return result
    except Exception as e:
        log.error(f"Analysis failed for {subject}: {e}")
        return {
            "classification": "system_status",
            "status": "ok",
            "summary": f"{subject} — analysis failed, review manually.",
            "action_required": False,
            "action_note": None,
            "subject": subject,
            "from": sender.get("address", "unknown"),
        }


# ---------------------------------------------------------------------------
# Final summary generation
# ---------------------------------------------------------------------------

BRIEFING_PROMPT = """
You are Artighost, an IT helper agent for Roland Foods.
Generate a concise morning IT briefing from the analyzed emails below.

Format:
*Overall:* one sentence health assessment (use ✅ if all clear, ⚠️ if warnings, 🔴 if errors)

*Financial Reports:*
- List each with specific numbers and whether they match or flag discrepancy

*Order Mismatches:*
- State count or confirm clean

*Process Failures:*
- Summarize each failure

*Action Required:*
- List items or state "None"

*Summary:* X emails analyzed, Y ignored

Be specific and factual. Use the numbers and details from the analyses provided.
""".strip()


def generate_briefing(analyses: list, ignored_count: int, date_str: str) -> str:
    relevant = [a for a in analyses if a.get("classification") != "ignore"]

    if not relevant:
        return (
            f"📋 *Morning IT Briefing* — {date_str}\n"
            f"✅ No system monitoring emails received in the overnight window.\n"
            f"_{ignored_count} email(s) ignored as non-IT-relevant._"
        )

    context = json.dumps(relevant, indent=2)
    total = len(analyses)

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-1",
            messages=[
                {"role": "system", "content": BRIEFING_PROMPT},
                {"role": "user", "content": f"Date: {date_str}\nAnalyses:\n{context}\nTotal: {total}, Ignored: {ignored_count}"},
            ],
            max_tokens=800,
            temperature=0.3,
        )
        body = response.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"Briefing generation failed: {e}")
        body = "Briefing generation failed — check logs."

    return f"📋 *Morning IT Briefing* — {date_str}\n\n{body}"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def post_to_slack(message: str) -> None:
    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    try:
        slack.chat_postMessage(channel=SUMMARY_CHANNEL, text=message)
        log.info("Summary posted to Slack.")
    except SlackApiError as e:
        log.error(f"Slack post failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(test_mode: bool = False) -> None:
    log.info(f"Daily summary starting {'(TEST MODE — last 8 hours)' if test_mode else '(12AM-8AM Eastern)'}")

    since, until = get_time_window(test_mode)
    emails = fetch_emails_in_window(since, until)
    date_str = since.astimezone(EASTERN).strftime("%B %d, %Y")

    if not emails:
        post_to_slack(
            f"📋 *Morning IT Briefing* — {date_str}\n"
            f"No unread emails in the monitoring window."
        )
        log.info("No emails — posted empty briefing.")
        return

    # Analyze each email individually with vision
    analyses = []
    for email in emails:
        log.info(f"Analyzing: {email.get('subject', '(no subject)')}")
        images = extract_inline_images(email)
        analysis = analyze_email(email, images)
        analyses.append(analysis)
        log.info(f"  → {analysis['classification']} | {analysis['status']} | {analysis['summary'][:80]}")

    ignored_count = sum(1 for a in analyses if a.get("classification") == "ignore")
    briefing = generate_briefing(analyses, ignored_count, date_str)
    post_to_slack(briefing)

    # Mark all as read
    for email in emails:
        try:
            mark_as_read(email["id"])
        except Exception as e:
            log.warning(f"Could not mark {email['id']} as read: {e}")

    log.info(f"Done. {len(emails)} email(s) processed, {ignored_count} ignored.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artighost Daily Summary")
    parser.add_argument("--now", action="store_true", help="Run immediately using last 8 hours")
    args = parser.parse_args()
    run(test_mode=args.now)
