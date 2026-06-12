"""
Permissions module.
Controls who can do what, and which mailboxes the agent can touch.

Action tiers:
  SELF_SERVICE — any authenticated Slack user (e.g. open a ticket for themselves)
  ADMIN_ONLY   — only users listed in ADMIN_USER_IDS

Mailbox access:
  Hard-locked to AGENT_EMAIL only. Any attempt to access another mailbox
  raises PermissionError regardless of Graph API permissions.
"""

import os
import logging

log = logging.getLogger(__name__)

# Actions any Slack user can trigger
SELF_SERVICE_ACTIONS = {
    "open_ticket",       # open a ticket on their own behalf
    "ask_question",      # Q&A against KB
    "check_status",      # check status of their own ticket
}

# Actions restricted to admins only
ADMIN_ACTIONS = {
    "read_email",
    "close_ticket",
    "assign_ticket",
    "query_user",
    "list_tickets",
    "send_email",
}


def get_admins() -> list[str]:
    return [
        uid.strip()
        for uid in os.environ.get("ADMIN_USER_IDS", "").split(",")
        if uid.strip()
    ]


def is_admin(user_id: str) -> bool:
    return user_id.strip() in get_admins()


def can_perform(user_id: str, action: str) -> bool:
    """
    Check if a user is allowed to perform an action.
    Logs denied attempts.
    """
    if action in SELF_SERVICE_ACTIONS:
        return True

    if action in ADMIN_ACTIONS:
        if is_admin(user_id):
            return True
        log.warning(f"Permission denied: user {user_id} attempted admin action '{action}'")
        return False

    # Unknown action — deny by default
    log.warning(f"Permission denied: unknown action '{action}' requested by {user_id}")
    return False


# ---------------------------------------------------------------------------
# Mailbox allowlist — hard boundary, not configurable at runtime
# ---------------------------------------------------------------------------

def get_allowed_mailboxes() -> set[str]:
    agent_email = os.environ.get("AGENT_EMAIL", "").lower()
    if not agent_email:
        raise EnvironmentError("AGENT_EMAIL is not set.")
    return {agent_email}


def safe_mailbox(mailbox: str) -> str:
    """
    Validate that a mailbox is on the allowlist before any Graph API call.
    Raises PermissionError if not allowed.
    """
    allowed = get_allowed_mailboxes()
    if mailbox.lower() not in allowed:
        raise PermissionError(
            f"Mailbox '{mailbox}' is not in the allowed list. "
            f"Allowed: {allowed}"
        )
    return mailbox
