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


def profit_loss_email_exists_in_window(since: datetime, until: datetime) -> bool:
    """
    Dedicated presence check for the Profit Loss email, independent of read state.
    The early alert check may have already read other emails in this window,
    so the main unread-only fetch can't be trusted for this specific check.
    """
    mailbox = os.environ["AGENT_EMAIL"]
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    data = graph_get(
        f"/users/{mailbox}/mailFolders/inbox/messages",
        params={
            "$filter": (
                f"receivedDateTime ge {since_str} "
                f"and receivedDateTime le {until_str} "
                f"and subject eq 'Profit Loss Data Update Statement'"
            ),
            "$top": 1,
            "$select": "id,subject",
        },
    )

    found = len(data.get("value", [])) > 0
    log.info(f"Profit Loss email presence check (read-state independent): {'FOUND' if found else 'NOT FOUND'}")
    return found


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
    from datetime import timedelta
    eastern = ZoneInfo("America/New_York")
    today = datetime.now(eastern).date()
    yesterday = today - timedelta(days=1)
    today_str = today.strftime("%A, %B %d, %Y")
    yesterday_str = yesterday.strftime("%A, %B %d, %Y")

    # Compute date check instructions in Python — no ambiguity for the model
    # weekday(): Monday=0, Tuesday=1, ..., Saturday=5, Sunday=6
    if yesterday.weekday() >= 5:
        pnl_date_check = "Skip this check entirely — yesterday was a weekend day. Do not flag anything date-related."
        ship_date_check = "Skip this check entirely — yesterday was a weekend day. Do not flag anything date-related."
    else:
        pnl_date_check = (
            f"The table called \"PnL Entry Date\" lists calendar dates. "
            f"Check whether {yesterday_str} is present anywhere in that list — yes or no. "
            f"Do not assume anything about its position (first, last, or otherwise). "
            f"If it is not present, flag this check as failed."
        )
        ship_date_check = (
            f"The table called \"Cases Ship - Invoice Date\" lists calendar dates. "
            f"Check whether {yesterday_str} is present anywhere in that list — yes or no. "
            f"Do not assume anything about its position (first, last, or otherwise). "
            f"If it is not present, flag this check as failed."
        )

    return f"""
You are analyzing a monitoring email received by the IT team at Roland Foods.

Today is {today_str}. Yesterday was {yesterday_str}.

Roland Foods receives these types of monitoring emails:
- Profit Loss Data Update Statement: This email has the exact subject "Profit Loss Data Update Statement". It is not spam, do not ignore it. In the body of the email, the number of vouchers will be mentioned twice. These numbers should match. Flag if they do not match.
- Last date in Tableau Extracts report. Perform THREE independent checks:
  1. NUMBER CHECK: The image contains two tables stacked vertically called "PnL Entry Date" and "Cases Ship - Invoice Date". Compare the "PnL Entra Date" Grand Total amount against the "Cases Ship - Invoice Date" Grand Total amount. If they differ by MORE THAN 1,000 (i.e. the difference exceeds 1,000), flag as warning. A difference of 1,001 or more is a flag. A difference of 1,000 or less is acceptable.
  2. PNL ENTRY DATE CHECK: {pnl_date_check}
  3. CASES SHIP INVOICE DATE CHECK: {ship_date_check}
  All three checks must pass independently for status to be "ok". Include the actual numbers you read from the image in your summary, and state clearly which of the three checks passed or failed.
- Process/job failure alerts. Summarize what failed and any available context.
- General system status updates.
- Spam or non-IT-relevant content.

Analyze the email including any images. Respond with JSON only:
{{
  "classification": "profit_loss|financial_report|process_failure|system_status|ignore",
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

    # Use full body content, not just the 255-char preview
    body_content = email.get("body", {}).get("content", "")
    body_type = email.get("body", {}).get("contentType", "text")

    if body_type.lower() == "html":
        # Strip HTML tags to get readable text, keep it reasonably sized
        soup = BeautifulSoup(body_content, "html.parser")
        body_text = soup.get_text(separator="\n").strip()
    else:
        body_text = body_content.strip()

    # Cap length to avoid excessive token usage, but much larger than the old 500-char preview
    body_text = body_text[:5000]

    text = (
        f"From: {sender.get('address', 'unknown')}\n"
        f"Subject: {subject}\n"
        f"Body:\n{body_text}"
    )

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
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-1"),
            messages=[
                {"role": "system", "content": build_email_analysis_prompt()},
                {"role": "user", "content": content},
            ],
            max_completion_tokens=400,
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
Generate a brief morning IT briefing from the analyzed emails below. Be concise — short bullets, no repeated numbers across sections, no restating the same fact twice.

Format:
*Overall:* one short sentence (✅ all clear, ⚠️ warnings, 🔴 critical — use 🔴 if a critical pipeline issue was flagged, even if other items are minor)

*Financial Reports:*
- One short bullet per report. State numbers once. If a check failed, say which check and why, briefly.

*Process Failures:*
- One short bullet per failure.

*Action Required:*
- Short list, or "None"

*Summary:* X emails analyzed, Y ignored

Be factual and terse. Do not repeat the same number or fact in multiple sections.
""".strip()


def generate_briefing(analyses: list, ignored_count: int, date_str: str, profit_loss_received: bool) -> str:
    relevant = [a for a in analyses if a.get("classification") != "ignore"]

    missing_pnl_flag = (
        ""
        if profit_loss_received
        else "\n\nNOTE: No email with subject \"Profit Loss Data Update Statement\" was received in this window. "
             "This is a CRITICAL issue — the Overall status must be 🔴 critical. "
             "In Action Required, include: \"Profit Loss Data pipeline may be incomplete.\""
    )

    if not relevant:
        overall = "🔴 Critical" if not profit_loss_received else "✅ All clear"
        pnl_line = (
            "\n\n*Action Required:*\n- Profit Loss Data pipeline may be incomplete."
            if not profit_loss_received else ""
        )
        return (
            f"📋 *Morning IT Briefing* — {date_str}\n"
            f"*Overall:* {overall}\n"
            f"No system monitoring emails received in the overnight window."
            f"{pnl_line}\n"
            f"_{ignored_count} email(s) ignored as non-IT-relevant._"
        )

    context = json.dumps(relevant, indent=2)
    total = len(analyses)

    try:
        response = openai_client.chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-1"),
            messages=[
                {"role": "system", "content": BRIEFING_PROMPT},
                {"role": "user", "content": f"Date: {date_str}\nAnalyses:\n{context}\nTotal: {total}, Ignored: {ignored_count}{missing_pnl_flag}"},
            ],
            max_completion_tokens=600,
            temperature=0.2,
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

    # Independent of read state — catches the case where the early check
    # already read the Profit Loss email before this run.
    profit_loss_received = profit_loss_email_exists_in_window(since, until)
    if not profit_loss_received:
        log.warning("Profit Loss Data Update Statement email NOT found in this window.")

    if not emails:
        overall = "🔴 Critical" if not profit_loss_received else "✅ All clear"
        action = (
            "\n\n*Action Required:*\n- Profit Loss Data pipeline may be incomplete."
            if not profit_loss_received else ""
        )
        post_to_slack(
            f"📋 *Morning IT Briefing* — {date_str}\n"
            f"*Overall:* {overall}\n"
            f"No unread emails in the monitoring window.{action}"
        )
        log.info("No unread emails — posted briefing.")
        return

    analyses = []
    for email in emails:
        log.info(f"Analyzing: {email.get('subject', '(no subject)')}")
        images = extract_inline_images(email)
        analysis = analyze_email(email, images)
        analyses.append(analysis)
        log.info(f"  → {analysis['classification']} | {analysis['status']} | {analysis['summary'][:80]}")

    ignored_count = sum(1 for a in analyses if a.get("classification") == "ignore")
    briefing = generate_briefing(analyses, ignored_count, date_str, profit_loss_received)
    post_to_slack(briefing)

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
