"""
Conversation handler.
Builds the prompt, calls Azure OpenAI with function calling,
executes tool calls, and returns a response string.

Currently supports one tool: create_ticket (FreshService).
Email/alert handling has been removed — email is now handled entirely
by daily_summary.py and early_alert_check.py on a schedule; there is
no in-conversation email flow.
"""

import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI
from config import load_config
from vectorstore import find_relevant_rules, search_knowledge
from freshservice import (
    create_ticket,
    create_problem,
    ticket_url,
    problem_url,
    update_ticket_description,
    update_problem_description,
)

log = logging.getLogger(__name__)

client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)

# Questions the bot must collect before creating an Incident ticket.
# "I don't know" is an acceptable answer; skipping is not.
INCIDENT_QUESTIONS = [
    "What application is impacted?",
    "Does a workaround exist, and if so what is it?",
    "Who is impacted (just you, your team, everyone)?",
    "What's the exact error message (screenshot if possible)?",
    "When did this start?",
]

# Questions the bot must collect before creating a Project (FreshService Problem).
PROJECT_QUESTIONS = [
    "What are the goals of this project? (Describe what you want to accomplish.)",
    "Is there a workaround being used today, or is this new capability?",
    "What's the expected ROI or business value?",
    "What system or application is this associated with?",
]

# Questions the bot must collect when the incident is a standard access /
# provisioning request (e.g. "I need access to Dynamics"). Uses the same
# create_incident_ticket tool with request_type="access_request".
ACCESS_REQUEST_QUESTIONS = [
    "What system or application do you need access to?",
    "What level of access do you need? (read-only, standard user, admin, or 'same as [colleague]')",
    "What will you be using it for?",
    "Is this replacing access you used to have, or is it new for your role?",
    "Who approves this?",
]

# Urgency keyword hints. Matched loosely by the model, not a strict regex.
URGENT_KEYWORDS = [
    "urgent", "critical", "emergency",
    "system down", "unable to work", "everyone is down", "can't work",
    "production down", "site down", "outage",
]

BASE_SYSTEM_PROMPT = f"""
You are Artighost, an IT helper agent for Roland Foods.
Your PRIMARY DIRECTIVE is to open well-informed FreshService tickets. You should
also try to help the user resolve simple issues in conversation, but do not
troubleshoot indefinitely — if the issue isn't obviously solvable in a couple
of exchanges, move toward opening a ticket instead.

Be concise and direct. When you're unsure, say so — don't guess.

Incident vs. Project classification (do this AS SOON AS the request looks
like it will need a ticket):

- INCIDENT: something impacting current state or existing systems is broken,
  not working, needs to be fixed, or a user needs standard access/provisioning
  to existing tools. Includes error messages, outages, "X isn't working",
  slowness, access requests to existing systems.
- PROJECT: any enhancement, new application, request for new functionality on
  an existing platform, or new build. Includes "can we add", "we should have",
  "would it be possible to", "we need a new tool for".

MIXED requests (something is broken AND the user is asking for an enhancement)
should be treated as an INCIDENT — collect incident info, open a ticket — and
at the end mention the enhancement piece and tell them to send you a separate
message about it so you can log it as a project.

Ticket-creation workflow (follow this exactly):

1. Once classified, decide whether to attempt resolution first:

   - For INCIDENTS: try to help the user resolve the issue conversationally
     BEFORE starting the question collection. Keep troubleshooting as long
     as you have new ideas that could plausibly help and the user is engaged.
     Move to question collection (step 2) when ANY of these becomes true:
       * The user explicitly asks you to open a ticket.
       * You have run out of things to try — you don't have another
         reasonable suggestion.
       * The request obviously requires human action (hardware replacement,
         account provisioning, access grants, approvals) — do not
         troubleshoot these.
       * The user tells you a step you suggested didn't work AND you have
         nothing else to try.

   - For PROJECTS: skip resolution entirely. Go straight to question
     collection — there is nothing to troubleshoot on an enhancement request.

2. Collect answers to every question in the appropriate set below. Ask them
   naturally — one at a time, or grouped where it flows. Do not skip any.

INCIDENT questions:
{chr(10).join(f"   - {q}" for q in INCIDENT_QUESTIONS)}

   If the incident is actually a standard ACCESS or PROVISIONING request
   (user needs access to an existing system, not something broken), use
   this question set INSTEAD of the regular incident questions above, and
   set request_type="access_request" on the tool call:

{chr(10).join(f"   - {q}" for q in ACCESS_REQUEST_QUESTIONS)}

PROJECT questions:
{chr(10).join(f"   - {q}" for q in PROJECT_QUESTIONS)}

3. "I don't know" is an acceptable answer and counts as answered. Skipping
   a question or ignoring it is NOT acceptable.

4. For INCIDENTS, also determine urgency based on the conversation. Default
   is Medium. Use High only if the user's description contains language
   like: {", ".join(URGENT_KEYWORDS)} — or clearly indicates a system-down
   or work-blocking situation. Use Low for informational requests, cosmetic
   issues, or things the user has explicitly said are not blocking. When
   unsure between two options, pick Medium.

5. Once every question has an answer:
   - For an INCIDENT, call create_incident_ticket with subject, summary,
     urgency ("low", "medium", or "high"), and answers (an object keyed
     by question text).
   - For a PROJECT, call create_project with subject, summary, and answers.

6. If the user asks you to open the ticket early, tell them you need a
   couple more answers first and continue asking.

7. After the ticket/project is created, tell the user the reference number,
   include the FreshService URL that gets returned, and add:
   "If you have screenshots or other files to attach, please add them
   directly to that link."

Image handling: if the user sends screenshots or images during the conversation,
describe what you observed in the summary field (error messages you read,
what the screenshot shows) so the human agent has that context.

If the user's message describes a new, unrelated issue from what was
previously discussed, treat it as a new incident/project — restart the
classification and question collection for that new item.

Format your responses for Slack, not for a Markdown document:
- Keep responses short. If a full answer would be long, give the 2-3 most
  important next steps and offer to go deeper if needed. Do not dump entire
  runbooks unprompted.
- Use *single asterisks* for bold, not **double asterisks** — Slack renders
  double asterisks as literal characters.
- Do not use Markdown headers like # or ###. If you need a section break,
  use a short bolded phrase on its own line.
- Prefer short flat bulleted lists. Avoid nesting bullets more than one level.
- Do not repeat the same recommendation across multiple sections.
- No closing summary or "let me know if..." lines unless genuinely useful.
""".strip()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_incident_ticket",
            "description": (
                "Create a FreshService ticket for an INCIDENT (something broken, "
                "access request, existing-system issue). Only call after collecting "
                "answers to every INCIDENT question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Short, specific subject line.",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "One-paragraph summary of the issue: what the user "
                            "reported, what troubleshooting was tried, current "
                            "state, and any relevant context from screenshots "
                            "shared during the conversation."
                        ),
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "'high' only for system-down/work-blocking cases "
                            "or explicit user language like urgent/critical/"
                            "emergency. 'low' for cosmetic or informational "
                            "requests the user has said aren't blocking. "
                            "Otherwise 'medium'."
                        ),
                    },
                    "request_type": {
                        "type": "string",
                        "enum": ["incident", "access_request"],
                        "description": (
                            "'access_request' for standard access/provisioning "
                            "requests to existing systems. 'incident' for "
                            "anything else (broken systems, errors, etc.)."
                        ),
                    },
                    "answers": {
                        "type": "object",
                        "description": (
                            "User's answers to each INCIDENT question, keyed by "
                            "the question text. Every question must be present. "
                            "'I don't know' is a valid answer."
                        ),
                    },
                },
                "required": ["subject", "summary", "urgency", "request_type", "answers"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": (
                "Create a FreshService Problem for a PROJECT (enhancement, new "
                "functionality, new application, new build). Only call after "
                "collecting answers to every PROJECT question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Short, specific project name / subject line.",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "One-paragraph summary describing the project as the "
                            "user has articulated it. Include what they want to "
                            "accomplish and why."
                        ),
                    },
                    "answers": {
                        "type": "object",
                        "description": (
                            "User's answers to each PROJECT question, keyed by "
                            "the question text. Every question must be present."
                        ),
                    },
                },
                "required": ["subject", "summary", "answers"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, arguments: dict, is_admin: bool, requester_email: str = None) -> str:
    if name == "create_incident_ticket":
        return _tool_create_incident(
            subject=arguments["subject"],
            summary=arguments["summary"],
            urgency=arguments.get("urgency", "medium"),
            request_type=arguments.get("request_type", "incident"),
            answers=arguments.get("answers", {}),
            requester_email=requester_email,
        )
    if name == "create_project":
        return _tool_create_project(
            subject=arguments["subject"],
            summary=arguments["summary"],
            answers=arguments.get("answers", {}),
            requester_email=requester_email,
        )
    return f"Unknown tool: {name}"


def _esc(s):
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>"))


def _format_body(summary: str, questions: list[str], answers: dict, attach_link: str) -> str:
    """
    Build a formatted HTML ticket/problem description with:
      - Summary paragraph
      - Structured Q&A section
      - Note pointing user to attach files at the given FreshService link
    """
    parts = ["<p><b>Summary</b></p>", f"<p>{_esc(summary)}</p>", "<hr>",
             "<p><b>Details</b></p>"]

    for question in questions:
        answer = answers.get(question)
        if answer is None:
            # Best-effort case-insensitive fallback if the model rephrased the key
            for k, v in answers.items():
                if k.strip().lower() == question.strip().lower():
                    answer = v
                    break
        if answer is None:
            answer = "(not provided)"
        parts.append(f"<p><b>{_esc(question)}</b><br>{_esc(answer)}</p>")

    parts.append("<hr>")
    parts.append(
        f'<p><i>If you have screenshots or other files to attach, please add '
        f'them directly to this record: <a href="{attach_link}">{attach_link}</a></i></p>'
    )
    return "\n".join(parts)


def _tool_create_incident(subject: str, summary: str, urgency: str, request_type: str, answers: dict, requester_email: str = None) -> str:
    if not requester_email:
        log.warning("create_incident_ticket called without a resolvable requester email.")
        return (
            "Could not create the ticket — no email address on file for this user. "
            "Ask them to email IT directly, or open the ticket manually."
        )

    # FreshService priority mapping: 1=Low, 2=Medium, 3=High, 4=Urgent (unused)
    priority_map = {"low": 1, "medium": 2, "high": 3}
    priority = priority_map.get(urgency.strip().lower(), 2)

    # Pick the question set to render in the ticket body based on request type.
    questions = ACCESS_REQUEST_QUESTIONS if request_type.strip().lower() == "access_request" else INCIDENT_QUESTIONS

    try:
        # Create with a placeholder attach link — we need the ID before we can
        # build the real URL, and the URL needs to be inside the description.
        placeholder_desc = _format_body(
            summary=summary,
            questions=questions,
            answers=answers,
            attach_link="(pending)",
        )
        ticket = create_ticket(
            subject=subject,
            description=placeholder_desc,
            requester_email=requester_email,
            priority=priority,
        )
        ticket_id = ticket.get("id")
        url = ticket_url(ticket_id)

        # Patch the description with the real URL now that we have the ID.
        final_desc = _format_body(
            summary=summary,
            questions=questions,
            answers=answers,
            attach_link=url,
        )
        try:
            update_ticket_description(ticket_id, final_desc)
        except Exception as e:
            log.warning(f"Ticket #{ticket_id} created but description update failed: {e}")

        return (
            f"Ticket #{ticket_id} created ({urgency}). "
            f"Reply to the user with this: 'Ticket #{ticket_id} has been opened: {url} "
            f"If you have screenshots or other files to attach, please add them there.'"
        )
    except Exception as e:
        log.error(f"Incident ticket creation failed: {e}")
        return "Failed to create the ticket — let the user know to contact IT directly."


def _tool_create_project(subject: str, summary: str, answers: dict, requester_email: str = None) -> str:
    if not requester_email:
        log.warning("create_project called without a resolvable requester email.")
        return (
            "Could not create the project — no email address on file for this user. "
            "Ask them to email IT directly, or open the project manually."
        )

    try:
        placeholder_desc = _format_body(
            summary=summary,
            questions=PROJECT_QUESTIONS,
            answers=answers,
            attach_link="(pending)",
        )

        # Map project answers → FreshService required custom fields.
        # If the model rephrased a question key, _get_answer falls back to
        # case-insensitive matching. Empty answers are safer than missing keys.
        def _get_answer(question_text: str) -> str:
            val = answers.get(question_text)
            if val is None:
                for k, v in answers.items():
                    if k.strip().lower() == question_text.strip().lower():
                        val = v
                        break
            return str(val) if val is not None else "Not provided"

        custom_fields = {
            "what_are_the_goals_of_this_project_provide_cost_benefit_details": _get_answer(PROJECT_QUESTIONS[0]),
            "do_you_have_an_existing_workaround": _get_answer(PROJECT_QUESTIONS[1]),
            "project_roi": _get_answer(PROJECT_QUESTIONS[2]),
        }

        problem = create_problem(
            subject=subject,
            description=placeholder_desc,
            requester_email=requester_email,
            custom_fields=custom_fields,
        )
        problem_id = problem.get("id")
        url = problem_url(problem_id)

        final_desc = _format_body(
            summary=summary,
            questions=PROJECT_QUESTIONS,
            answers=answers,
            attach_link=url,
        )
        try:
            update_problem_description(problem_id, final_desc)
        except Exception as e:
            log.warning(f"Problem #{problem_id} created but description update failed: {e}")

        return (
            f"Project #{problem_id} created. "
            f"Reply to the user with this: 'Project #{problem_id} has been logged: {url} "
            f"If you have supporting documents or screenshots, please add them there.'"
        )
    except Exception as e:
        log.error(f"Project creation failed: {e}")
        return "Failed to create the project — let the user know to contact IT directly."


# ---------------------------------------------------------------------------
# Slack user → email lookup
# ---------------------------------------------------------------------------

def _get_user_email(user_id: str, slack_client) -> str | None:
    """Look up a Slack user's email via users.info. Returns None if unavailable."""
    if not slack_client or not user_id:
        return None
    try:
        result = slack_client.users_info(user=user_id)
        return result.get("user", {}).get("profile", {}).get("email")
    except Exception as e:
        log.warning(f"Could not look up email for Slack user {user_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt(text: str, is_admin: bool) -> str:
    sections = [BASE_SYSTEM_PROMPT]

    if is_admin:
        sections.append("The current user is an IT admin and can request privileged actions.")
    else:
        sections.append(
            "The current user is a standard user. "
            "You can answer questions and open tickets on their behalf. "
            "Do not perform admin-only actions."
        )

    try:
        rules = find_relevant_rules(text, n=5)
        if rules:
            rule_lines = []
            for r in rules:
                pattern = r.get("pattern", "")
                note = r.get("note", "")
                rule_lines.append(
                    f"- {pattern}" + (f" ({note})" if note and note != pattern else "")
                )
            sections.append("## Relevant Rules\n" + "\n".join(rule_lines))
    except Exception as e:
        log.warning(f"Could not retrieve rules from ChromaDB: {e}")
        config = load_config()
        for r in config.get("rules", []):
            sections.append(f"## Rules\n- {r}")

    try:
        knowledge = search_knowledge(text, n=3, threshold=0.15)
        if knowledge:
            kb_lines = [f"- [{k['title']}] {k['text'][:300]}" for k in knowledge]
            sections.append("## Relevant Playbook Guidance\n" + "\n".join(kb_lines))
    except Exception as e:
        log.warning(f"Could not retrieve knowledge from ChromaDB: {e}")

    config = load_config()
    instructions = config.get("instructions", [])
    if instructions:
        sections.append("## Instructions\n" + "\n".join(f"- {i}" for i in instructions))

    return "\n\n".join(sections)


def _build_messages(
    text: str,
    thread_ts: str,
    channel: str,
    slack_client,
    is_admin: bool,
    images: list = None,
) -> list:
    prompt = build_system_prompt(text, is_admin)
    messages = [{"role": "system", "content": prompt}]

    # History — DMs are flat (not threaded), so pull recent channel history
    # in chronological order rather than a specific thread's replies.
    # Non-DM channels (mentions) still use thread replies.
    if slack_client and channel and channel != "email":
        try:
            if thread_ts:
                history = slack_client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    limit=10,
                )
                msgs = history.get("messages", [])[:-1]
            else:
                history = slack_client.conversations_history(
                    channel=channel,
                    limit=15,
                )
                msgs = list(reversed(history.get("messages", [])))[:-1]

            bot_id = slack_client.auth_test()["user_id"]
            for msg in msgs:
                role = "assistant" if msg.get("user") == bot_id else "user"
                content = msg.get("text", "")
                if content:
                    messages.append({"role": role, "content": content})
        except Exception as e:
            log.warning(f"Could not fetch conversation history: {e}")

    # Build user message — text + optional images
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
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": text})

    return messages


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_message(
    user_id: str,
    text: str,
    channel: str,
    thread_ts: str,
    client,
    is_admin: bool = False,
    images: list = None,
) -> str:
    messages = _build_messages(text, thread_ts, channel, client, is_admin, images)
    requester_email = _get_user_email(user_id, client)

    try:
        response = _call_openai(messages)
        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            messages.append(choice.message)

            for tool_call in choice.message.tool_calls:
                name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)

                log.info(f"Tool call: {name}({arguments})")
                result = execute_tool(
                    name,
                    arguments,
                    is_admin,
                    requester_email=requester_email,
                )
                log.info(f"Tool result: {result}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

            final_response = _call_openai(messages)
            return final_response.choices[0].message.content.strip()

        return choice.message.content.strip()

    except Exception as e:
        log.error(f"OpenAI call failed: {e}")
        return "Sorry, I ran into an error. Try again or ping IT directly."


def _call_openai(messages: list):
    return client.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4"),
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        max_completion_tokens=500,
        temperature=0.3,
    )
