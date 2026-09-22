"""
FreshService API helper — ticket and problem creation.
Reuses the same API key auth pattern as freshservice-export.py /
freshservice-backfill.py (FRESHSERVICE_API_KEY env var, basic auth
with the key as username and 'X' as password).

Note: Roland Foods maps "Incidents" to FreshService Tickets and
"Projects" to FreshService Problems — two different API resources.
"""

import os
import logging
import requests

log = logging.getLogger(__name__)

FRESHSERVICE_DOMAIN = "rolandfoods.freshservice.com"
BASE_URL = f"https://{FRESHSERVICE_DOMAIN}/api/v2"

# FreshService URL patterns for user-facing links.
TICKET_URL_TEMPLATE = f"https://{FRESHSERVICE_DOMAIN}/helpdesk/tickets/{{id}}"
PROBLEM_URL_TEMPLATE = f"https://{FRESHSERVICE_DOMAIN}/a/problems/{{id}}"


def _auth():
    return (os.environ["FRESHSERVICE_API_KEY"], "X")


def create_ticket(
    subject: str,
    description: str,
    requester_email: str,
    priority: int = 2,
) -> dict:
    """
    Create a FreshService ticket (Incident).

    priority: 1 = Low, 2 = Medium (default), 3 = High, 4 = Urgent.

    Returns the created ticket dict (includes 'id') on success.
    Raises requests.HTTPError on failure.
    """
    payload = {
        "subject": subject,
        "description": description,
        "email": requester_email,
        "status": 2,     # Open
        "priority": priority,
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


def create_problem(
    subject: str,
    description: str,
    requester_email: str,
    custom_fields: dict = None,
) -> dict:
    """
    Create a FreshService Problem (used at Roland Foods to track Projects —
    enhancements, new applications, new functionality requests).

    custom_fields: dict of custom field API names to values. Roland Foods'
    Problem form requires the following:
      - what_are_the_goals_of_this_project_provide_cost_benefit_details
      - project_roi
      - do_you_have_an_existing_workaround

    Returns the created problem dict (includes 'id') on success.
    Raises requests.HTTPError on failure.
    """
    payload = {
        "subject": subject,
        "description": description,
        "email": requester_email,
        "status": 1,     # Open
        "priority": 2,   # Medium — projects don't carry urgency the same way
    }
    if custom_fields:
        payload["custom_fields"] = custom_fields

    resp = requests.post(
        f"{BASE_URL}/problems",
        json=payload,
        auth=_auth(),
    )

    if resp.status_code >= 400:
        log.error(f"FreshService problem creation failed ({resp.status_code}): {resp.text}")
        resp.raise_for_status()

    problem = resp.json().get("problem", {})
    log.info(f"Problem created: #{problem.get('id')} — {subject}")
    return problem


def ticket_url(ticket_id: int) -> str:
    return TICKET_URL_TEMPLATE.format(id=ticket_id)


def problem_url(problem_id: int) -> str:
    return PROBLEM_URL_TEMPLATE.format(id=problem_id)


def update_ticket_description(ticket_id: int, description: str) -> None:
    """Overwrite the description on an existing ticket."""
    resp = requests.put(
        f"{BASE_URL}/tickets/{ticket_id}",
        json={"description": description},
        auth=_auth(),
    )
    if resp.status_code >= 400:
        log.error(f"Ticket #{ticket_id} description update failed ({resp.status_code}): {resp.text}")
        resp.raise_for_status()


def update_problem_description(problem_id: int, description: str) -> None:
    """Overwrite the description on an existing problem."""
    resp = requests.put(
        f"{BASE_URL}/problems/{problem_id}",
        json={"description": description},
        auth=_auth(),
    )
    if resp.status_code >= 400:
        log.error(f"Problem #{problem_id} description update failed ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
