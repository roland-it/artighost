"""
FreshService API helper — ticket creation.
Reuses the same API key auth pattern as freshservice-export.py /
freshservice-backfill.py (FRESHSERVICE_API_KEY env var, basic auth
with the key as username and 'X' as password).
"""

import os
import logging
import requests

log = logging.getLogger(__name__)

FRESHSERVICE_DOMAIN = "rolandfoods.freshservice.com"
BASE_URL = f"https://{FRESHSERVICE_DOMAIN}/api/v2"


def _auth():
    return (os.environ["FRESHSERVICE_API_KEY"], "X")


def create_ticket(subject: str, description: str, requester_email: str) -> dict:
    """
    Create a FreshService ticket. No category/group assignment yet —
    that's a deliberate Phase 1 simplification; tickets land unclassified
    for manual triage until auto-categorization is added.

    Returns the created ticket dict (includes 'id') on success.
    Raises requests.HTTPError on failure.
    """
    payload = {
        "subject": subject,
        "description": description,
        "email": requester_email,
        "status": 2,     # Open
        "priority": 1,   # Low — default; not inferring urgency yet
        "source": 2,     # Portal (closest generic fit; no "chatbot" source code)
        "responder_id": int(os.environ["FRESHSERVICE_DEFAULT_RESPONDER_ID"]),
        "group_id": int(os.environ["FRESHSERVICE_DEFAULT_GROUP_ID"]),
    }

    resp = requests.post(
        f"{BASE_URL}/tickets",
        json=payload,
        auth=_auth(),
    )

    if resp.status_code >= 400:
        log.error(f"FreshService ticket creation failed ({resp.status_code}): {resp.text}")
        resp.raise_for_status()

    ticket = resp.json().get("ticket", {})
    log.info(f"Ticket created: #{ticket.get('id')} — {subject}")
    return ticket
