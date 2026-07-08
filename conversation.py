"""
Conversation handler.
Builds the prompt, calls Azure OpenAI with function calling,
executes tool calls, and returns a response string.

Supports vision — images passed from the email poller are included
as base64 content blocks in the user message.

Alert routing:
  - No matching rule or training mode → TRAINING_CHANNEL_ID
  - Live mode rule matched           → LIVE_CHANNEL_ID
"""

import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI
from config import load_config
from vectorstore import find_relevant_rules

log = logging.getLogger(__name__)

client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)

BASE_SYSTEM_PROMPT = """
You are Artighost, an IT helper agent for Roland Foods.
You assist IT staff and end users with technical questions and requests.
You have access to tools and should use them when appropriate.
Be concise and direct. When you're unsure, say so — don't guess.
If asked to do something you cannot do, say so clearly and suggest contacting IT directly.

When you receive an email notification, always call send_slack_alert.
Include a one-line summary of the email and the sender in the alert message.
Do not ask for confirmation before sending.

If an email contains images, analyze them as part of your assessment.
Apply any relevant instructions about image content before deciding on urgency.
""".strip()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_slack_alert",
            "description": (
                "Send an alert message about an email or request. "
                "Always call this when processing an email. "
                "The channel is determined automatically based on rule mode."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Concise alert — sender, subject, one-line summary.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Urgency level.",
                    },
                },
                "required": ["message", "urgency"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, arguments: dict, is_admin: bool, rule_mode: str = "training") -> str:
    if name == "send_slack_alert":
        return _tool_send_slack_alert(
            message=arguments["message"],
            urgency=arguments.get("urgency", "medium"),
            rule_mode=rule_mode,
        )
    return f"Unknown tool: {name}"


def _tool_send_slack_alert(message: str, urgency: str, rule_mode: str = "training") -> str:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    if rule_mode == "live":
        channel = os.environ.get("LIVE_CHANNEL_ID")
        if not channel:
            log.warning("LIVE_CHANNEL_ID not set — falling back to training channel.")
            channel = os.environ.get("TRAINING_CHANNEL_ID")
    else:
        channel = os.environ.get("TRAINING_CHANNEL_ID")

    if not channel:
        log.warning("No alert channel configured.")
        return "No alert channel configured."

    urgency_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(urgency, "🟠")
    mode_tag = "🎓 *Training*" if rule_mode == "training" else "🔴 *Live*"
    full_message = f"{urgency_emoji} {mode_tag} [{urgency.upper()}]\n{message}"

    try:
        slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        slack.chat_postMessage(channel=channel, text=full_message)
        log.info(f"Alert sent to {rule_mode} channel: {message}")
        return f"Alert sent to {rule_mode} channel ({urgency} urgency)."
    except SlackApiError as e:
        log.error(f"Slack alert failed: {e}")
        return f"Failed to send alert: {e.response['error']}"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt(text: str, is_admin: bool) -> tuple[str, str]:
    sections = [BASE_SYSTEM_PROMPT]

    if is_admin:
        sections.append("The current user is an IT admin and can request privileged actions.")
    else:
        sections.append(
            "The current user is a standard user. "
            "You can answer questions and open tickets on their behalf. "
            "Do not perform admin-only actions."
        )

    rule_mode = "training"
    try:
        rules = find_relevant_rules(text, n=5)
        if rules:
            if any(r.get("mode") == "live" for r in rules):
                rule_mode = "live"
            rule_lines = []
            for r in rules:
                pattern = r.get("pattern", "")
                note = r.get("note", "")
                mode = r.get("mode", "training")
                rule_lines.append(
                    f"- [{mode.upper()}] {pattern}" +
                    (f" ({note})" if note and note != pattern else "")
                )
            sections.append("## Relevant Rules\n" + "\n".join(rule_lines))
    except Exception as e:
        log.warning(f"Could not retrieve rules from ChromaDB: {e}")
        config = load_config()
        for r in config.get("rules", []):
            sections.append(f"## Rules\n- {r}")

    config = load_config()
    instructions = config.get("instructions", [])
    if instructions:
        sections.append("## Instructions\n" + "\n".join(f"- {i}" for i in instructions))

    return "\n\n".join(sections), rule_mode


def _build_messages(
    text: str,
    thread_ts: str,
    channel: str,
    slack_client,
    is_admin: bool,
    images: list = None,
) -> tuple[list, str]:
    prompt, rule_mode = build_system_prompt(text, is_admin)
    messages = [{"role": "system", "content": prompt}]

    # Thread history (Slack only)
    if slack_client and channel and thread_ts and channel != "email":
        try:
            history = slack_client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=10,
            )
            bot_id = slack_client.auth_test()["user_id"]
            for msg in history.get("messages", [])[:-1]:
                role = "assistant" if msg.get("user") == bot_id else "user"
                content = msg.get("text", "")
                if content:
                    messages.append({"role": role, "content": content})
        except Exception as e:
            log.warning(f"Could not fetch thread history: {e}")

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

    return messages, rule_mode


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
    messages, rule_mode = _build_messages(text, thread_ts, channel, client, is_admin, images)

    try:
        response = _call_openai(messages)
        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            messages.append(choice.message)

            for tool_call in choice.message.tool_calls:
                name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)

                log.info(f"Tool call: {name}({arguments}) [mode={rule_mode}]")
                result = execute_tool(name, arguments, is_admin, rule_mode=rule_mode)
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
        max_completion_tokens=1000,
        temperature=0.3,
    )
