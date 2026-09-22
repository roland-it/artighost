"""
Daily RESOLVED BY BOT ticket creation.

Runs once a day (target: 8 PM Eastern via Rundeck).
Scans all DM conversations Artighost had today, groups messages by topic
via GPT, and creates one FreshService ticket per detected topic per user —
subject prefixed with "RESOLVED BY BOT".

Skips users who already have an escalation ticket logged for the day
(matched by requester email + creation date), so users whose issues
became real tickets don't get double-logged.

Usage:
  python daily_resolution_tickets.py          # runs normally, today's window
  python daily_resolution_tickets.py --now    # bypasses time check
"""

import os
import json
import time
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

from freshservice import create_ticket, create_problem

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
FRESHSERVICE_BASE = "https://rolandfoods.freshservice.com/api/v2"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# --- DRY RUN TOGGLE ---
# When True, no FreshService tickets are created. Instead, a single summary
# email is sent to DRY_RUN_RECIPIENT with everything that WOULD have been
# created. Flip to False to go live.
DRY_RUN = True
DRY_RUN_RECIPIENT = os.environ.get("DRY_RUN_RECIPIENT", "paulc_admin@rolandfoods.com")

openai_client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])


# ---------------------------------------------------------------------------
# Graph API — send dry-run email (same auth pattern as daily_summary.py)
# ---------------------------------------------------------------------------

_token_cache: dict = {"token": None, "expires_at": 0}


def get_graph_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    tenant_id = os.environ["ENTRA_TENANT_ID"]
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["ENTRA_CLIENT_ID"],
            "client_secret": os.environ["ENTRA_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data["expires_in"]
    return _token_cache["token"]


def send_dry_run_email(subject: str, html_body: str) -> None:
    mailbox = os.environ["AGENT_EMAIL"]
    token = get_graph_token()
    resp = requests.post(
        f"{GRAPH_BASE}/users/{mailbox}/sendMail",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": DRY_RUN_RECIPIENT}}],
            },
            "saveToSentItems": "false",
        },
    )
    resp.raise_for_status()
    log.info(f"Dry-run email sent to {DRY_RUN_RECIPIENT}")


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------

def get_day_window() -> tuple[datetime, datetime]:
    """Today, midnight to 8 PM Eastern, expressed as UTC datetimes."""
    now_eastern = datetime.now(EASTERN)
    today = now_eastern.date()
    since = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    until = datetime(today.year, today.month, today.day, 20, 0, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    return since, until


# ---------------------------------------------------------------------------
# Slack — pull bot DM activity
# ---------------------------------------------------------------------------

def list_bot_dms() -> list[dict]:
    """Return all IM channels the bot has open."""
    dms = []
    cursor = None
    while True:
        try:
            resp = slack_client.conversations_list(
                types="im",
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            log.error(f"conversations.list failed: {e}")
            break
        dms.extend(resp.get("channels", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return dms


def fetch_dm_messages(channel_id: str, since_ts: float, until_ts: float, bot_user_id: str) -> list[dict]:
    """
    Return chronological list of messages in this DM within [since, until],
    each tagged with role='user' or role='assistant' based on sender.
    """
    messages = []
    cursor = None
    while True:
        try:
            resp = slack_client.conversations_history(
                channel=channel_id,
                oldest=str(since_ts),
                latest=str(until_ts),
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            log.warning(f"conversations.history failed for {channel_id}: {e}")
            return []
        for msg in resp.get("messages", []):
            text = msg.get("text", "")
            if not text:
                continue
            if msg.get("subtype"):  # skip joins/edits/etc.
                continue
            role = "assistant" if msg.get("user") == bot_user_id else "user"
            messages.append({
                "role": role,
                "text": text,
                "ts": float(msg.get("ts", 0)),
            })
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break
    # Slack returns newest-first; reverse for chronological order.
    return list(reversed(messages))


def get_user_email(user_id: str) -> str | None:
    try:
        result = slack_client.users_info(user=user_id)
        return result.get("user", {}).get("profile", {}).get("email")
    except SlackApiError as e:
        log.warning(f"Could not resolve email for {user_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# FreshService — fetch today's tickets/problems for per-topic dedup
# ---------------------------------------------------------------------------

def fetch_todays_records(email: str, day_eastern: datetime.date) -> list[dict]:
    """
    Return list of {type, subject, description} for tickets AND problems
    the user has from today. Used by topic_matches_existing() to decide
    whether a detected RESOLVED BY BOT topic is really new or a duplicate
    of something already in FreshService.
    """
    day_str = day_eastern.isoformat()
    records = []

    for endpoint, key, rec_type in (
        ("/tickets", "tickets", "ticket"),
        ("/problems", "problems", "problem"),
    ):
        try:
            resp = requests.get(
                f"{FRESHSERVICE_BASE}{endpoint}",
                params={"email": email, "per_page": 30},
                auth=(os.environ["FRESHSERVICE_API_KEY"], "X"),
            )
            if resp.status_code >= 400:
                log.warning(f"FreshService {endpoint} query for {email} failed: {resp.status_code} {resp.text}")
                continue
            found = resp.json().get(key, [])
        except Exception as e:
            log.warning(f"FreshService {endpoint} query error for {email}: {e}")
            continue

        for r in found:
            created = r.get("created_at", "")
            if created.startswith(day_str):
                records.append({
                    "type": rec_type,
                    "subject": r.get("subject", ""),
                    "description": (r.get("description_text") or r.get("description") or "")[:800],
                })

    return records


DEDUP_PROMPT = """
You are comparing a newly-detected conversation topic against existing
FreshService tickets and problems the same user opened today.

Decide whether the new topic is the SAME UNDERLYING ISSUE as any of the
existing records. Phrasing may differ; use judgment about the actual
subject matter. If the new topic is clearly about something different,
answer no.

Return ONLY valid JSON, no prose, no code fences:
{
  "is_duplicate": true|false
}
""".strip()


def topic_matches_existing(new_subject: str, new_transcript: str, existing_records: list[dict]) -> bool:
    """
    Ask GPT whether a newly-detected RESOLVED BY BOT topic matches any of
    the user's existing records from today. Returns True → skip (duplicate),
    False → log as new. On any error, defaults to False (log it — safer
    to have an occasional duplicate than to silently drop legit topics).
    """
    if not existing_records:
        return False

    existing_lines = []
    for r in existing_records:
        existing_lines.append(f"[{r['type']}] {r['subject']}\n{r['description']}")
    existing_text = "\n\n---\n\n".join(existing_lines)

    user_content = (
        f"NEW TOPIC:\nSubject: {new_subject}\nTranscript excerpt:\n{new_transcript[:1500]}"
        f"\n\n===\n\nEXISTING RECORDS TODAY:\n{existing_text}"
    )

    try:
        resp = openai_client.chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4"),
            messages=[
                {"role": "system", "content": DEDUP_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=50,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return bool(json.loads(raw).get("is_duplicate", False))
    except Exception as e:
        log.warning(f"Dedup check failed for '{new_subject}', defaulting to not-duplicate: {e}")
        return False


# ---------------------------------------------------------------------------
# Topic grouping via GPT
# ---------------------------------------------------------------------------

GROUPING_PROMPT = """
You are analyzing a day's worth of chat messages between a user and an IT
helper bot (Artighost). Group the messages into distinct issues/topics,
and classify each group as an INCIDENT or a PROJECT.

- INCIDENT: something broken, not working, access request, error, existing-
  system issue.
- PROJECT: enhancement, new application, new functionality request, new build.

Two exchanges hours apart on the same underlying subject should be ONE group.
Two exchanges close in time about different subjects should be TWO groups.

Return ONLY valid JSON, no prose, no code fences:
{
  "issues": [
    {
      "subject": "short specific description of this issue",
      "type": "incident" | "project",
      "message_indices": [0, 1, 2, 3]
    },
    ...
  ]
}

message_indices refers to the 0-based position in the transcript below.
Every message must belong to exactly one group. Groups must be in the order
they first appear.
""".strip()


def group_by_topic(messages: list[dict]) -> list[dict]:
    """
    Ask GPT to group the day's messages into distinct issues.
    Returns list of {subject, message_indices}.
    On failure, falls back to one group containing everything.
    """
    if not messages:
        return []

    transcript_lines = []
    for i, m in enumerate(messages):
        transcript_lines.append(f"[{i}] {m['role']}: {m['text']}")
    transcript = "\n".join(transcript_lines)

    try:
        resp = openai_client.chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4"),
            messages=[
                {"role": "system", "content": GROUPING_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_completion_tokens=800,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        issues = parsed.get("issues", [])
        if not issues:
            raise ValueError("No issues returned")
        return issues
    except Exception as e:
        log.warning(f"Topic grouping failed, using single-group fallback: {e}")
        return [{
            "subject": "Bot conversation",
            "type": "incident",
            "message_indices": list(range(len(messages))),
        }]


# ---------------------------------------------------------------------------
# Transcript formatting for ticket body
# ---------------------------------------------------------------------------

def format_transcript(messages: list[dict], indices: list[int]) -> str:
    """Render a subset of messages as a readable HTML transcript for the ticket body."""
    lines = ["<p><b>Conversation transcript:</b></p>"]
    for i in indices:
        if i >= len(messages):
            continue
        m = messages[i]
        speaker = "Artighost" if m["role"] == "assistant" else "User"
        ts_str = datetime.fromtimestamp(m["ts"], tz=EASTERN).strftime("%I:%M %p")
        # basic HTML escape for the message text
        safe_text = (m["text"]
                     .replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;")
                     .replace("\n", "<br>"))
        lines.append(f"<p><b>{speaker}</b> ({ts_str}): {safe_text}</p>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(test_mode: bool = False) -> None:
    since, until = get_day_window()
    if test_mode:
        # widen window to catch anything from last 24h
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=24)

    day_eastern = since.astimezone(EASTERN).date()
    since_ts = since.timestamp()
    until_ts = until.timestamp()

    log.info(f"Scanning DM activity {since.isoformat()} to {until.isoformat()}")

    try:
        bot_user_id = slack_client.auth_test()["user_id"]
    except SlackApiError as e:
        log.error(f"Could not resolve bot user_id: {e}")
        return

    dms = list_bot_dms()
    log.info(f"Found {len(dms)} DM channels.")

    tickets_created = 0
    dry_run_drafts = []  # list of {email, subject, description}
    users_skipped = 0
    users_no_activity = 0

    for dm in dms:
        channel_id = dm.get("id")
        user_id = dm.get("user")
        if not channel_id or not user_id:
            continue

        messages = fetch_dm_messages(channel_id, since_ts, until_ts, bot_user_id)
        if not messages:
            users_no_activity += 1
            continue

        # Only consider conversations where the user actually said something
        # (bot-only channels with no user messages aren't real conversations).
        if not any(m["role"] == "user" for m in messages):
            users_no_activity += 1
            continue

        email = get_user_email(user_id)
        if not email:
            log.info(f"Skipping {user_id} — no email resolvable.")
            users_skipped += 1
            continue

        # Fetch this user's existing tickets/problems from today ONCE.
        # Per-topic dedup uses this list to decide skip vs. log.
        existing_records = fetch_todays_records(email, day_eastern)

        issues = group_by_topic(messages)
        log.info(f"{email}: {len(messages)} messages → {len(issues)} topic group(s); {len(existing_records)} existing record(s) today")

        for issue in issues:
            issue_type = issue.get("type", "incident").lower()
            base_subject = issue.get('subject', 'Bot conversation')
            subject = f"RESOLVED BY BOT: {base_subject}"
            description = format_transcript(messages, issue.get("message_indices", []))

            # Per-topic dedup: is this topic the same as any existing record?
            if existing_records and topic_matches_existing(base_subject, description, existing_records):
                log.info(f"  → skipping duplicate topic '{base_subject}' — matches existing record")
                continue

            if DRY_RUN:
                dry_run_drafts.append({
                    "email": email,
                    "subject": subject,
                    "type": issue_type,
                    "description": description,
                })
                log.info(f"  → DRY RUN: would create {issue_type} for {email}")
                continue

            try:
                if issue_type == "project":
                    record = create_problem(
                        subject=subject,
                        description=description,
                        requester_email=email,
                    )
                    tickets_created += 1
                    log.info(f"  → project #{record.get('id')} created")
                else:
                    record = create_ticket(
                        subject=subject,
                        description=description,
                        requester_email=email,
                    )
                    tickets_created += 1
                    log.info(f"  → ticket #{record.get('id')} created")
            except Exception as e:
                log.error(f"  → {issue_type} creation failed: {e}")

        # Small pause to be polite to both Slack and FreshService.
        time.sleep(0.2)

    if DRY_RUN:
        if dry_run_drafts:
            email_subject = f"[DRY RUN] Artighost — {len(dry_run_drafts)} RESOLVED BY BOT ticket(s) would have been created ({day_eastern.isoformat()})"
            sections = [
                f"<p>Dry run only. No FreshService tickets were created. "
                f"{len(dry_run_drafts)} would have been.</p>",
                f"<p><b>Users skipped (already had a ticket today or no email):</b> {users_skipped}<br>"
                f"<b>DMs with no activity in window:</b> {users_no_activity}</p>",
                "<hr>",
            ]
            for i, d in enumerate(dry_run_drafts, 1):
                type_label = d.get("type", "incident").upper()
                sections.append(
                    f"<h3>#{i} — {d['email']} — [{type_label}]</h3>"
                    f"<p><b>Subject:</b> {d['subject']}</p>"
                    f"{d['description']}"
                    f"<hr>"
                )
            try:
                send_dry_run_email(email_subject, "\n".join(sections))
            except Exception as e:
                log.error(f"Dry-run email failed: {e}")
        else:
            log.info("Dry run: nothing to report — no ticket drafts generated.")

    log.info(
        f"Done. Tickets created: {tickets_created} | Dry-run drafts: {len(dry_run_drafts)} | "
        f"Users skipped: {users_skipped} | DMs with no activity: {users_no_activity}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily RESOLVED BY BOT ticket logger")
    parser.add_argument("--now", action="store_true", help="Bypass day window, use last 24h")
    args = parser.parse_args()
    run(test_mode=args.now)
