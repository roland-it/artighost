"""
Conversation handler.
Builds the prompt, calls Azure OpenAI, returns a response string.
"""

import os
import logging
from openai import AzureOpenAI
from config import load_config

log = logging.getLogger(__name__)

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-02-01",
)

DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-1")

BASE_SYSTEM_PROMPT = """
You are an IT helper agent for Roland Foods. You assist IT staff and end users
with technical questions, ticket triage, and system information.

You have access to internal knowledge and can take actions when asked.
Be concise and direct. When you're unsure, say so — don't guess.

If a user asks you to perform an action you're not yet capable of, acknowledge it
and suggest they contact IT directly.
""".strip()


def build_system_prompt() -> str:
    """Combine base prompt with any admin-defined rules."""
    config = load_config()
    rules = config.get("rules", [])
    instructions = config.get("instructions", [])

    sections = [BASE_SYSTEM_PROMPT]

    if rules:
        rule_text = "\n".join(f"- {r}" for r in rules)
        sections.append(f"## Rules\n{rule_text}")

    if instructions:
        instr_text = "\n".join(f"- {i}" for i in instructions)
        sections.append(f"## Additional Instructions\n{instr_text}")

    return "\n\n".join(sections)


def handle_message(
    user_id: str,
    text: str,
    channel: str,
    thread_ts: str,
    client: object,  # Slack client, reserved for future context fetching
) -> str:
    """
    Process a user message and return the agent's response.
    Thread history is fetched so the agent has conversation context.
    """
    messages = _build_messages(text, thread_ts, channel, client)

    try:
        response = _call_openai(messages)
        return response
    except Exception as e:
        log.error(f"OpenAI call failed: {e}")
        return "Sorry, I ran into an error. Try again or ping IT directly."


def _build_messages(text: str, thread_ts: str, channel: str, slack_client) -> list:
    """Build the messages array including thread history."""
    messages = [{"role": "system", "content": build_system_prompt()}]

    # Fetch thread history for context (up to 10 prior messages)
    try:
        history = slack_client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=10,
        )
        bot_id = slack_client.auth_test()["user_id"]

        for msg in history.get("messages", [])[:-1]:  # exclude the triggering message
            role = "assistant" if msg.get("user") == bot_id else "user"
            content = msg.get("text", "")
            if content:
                messages.append({"role": role, "content": content})
    except Exception as e:
        log.warning(f"Could not fetch thread history: {e}")

    messages.append({"role": "user", "content": text})
    return messages


def _call_openai(messages: list) -> str:
    """Call Azure OpenAI and return the text response."""
    response = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=messages,
        max_tokens=1000,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()
