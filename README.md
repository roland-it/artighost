# IT Helper Agent

Slack-based IT assistant backed by Azure OpenAI (GPT-4o).
Responds to mentions and DMs. Trained via Slack commands.

## Structure

```
agent.py          # Entry point, Slack Bolt event handlers
conversation.py   # Azure OpenAI call, prompt construction, thread history
admin.py          # !command handler for rules/instructions
config.py         # JSON persistence for rules/instructions
agent_config.json # Auto-created on first save
.env              # Secrets (copy from .env.example)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env values
python agent.py
```

## Slack App Requirements

In api.slack.com, your app needs:
- **Socket Mode** enabled
- **Bot Token Scopes:** `app_mentions:read`, `channels:history`, `chat:write`,
  `groups:history`, `im:history`, `im:read`, `im:write`
- **Event Subscriptions:** `app_mention`, `message.im`, `message.channels`, `message.groups`

## Training the Agent via Slack

Admins (user IDs in `ADMIN_USER_IDS`) can DM the bot or post in the admin channel:

```
!rule add if someone mentions "locked out" or "can't log in", suggest a password reset
!rule add vendor notification emails do not need a ticket
!instruct add always sign off responses with "— IT Helper"
!rule list
!rule remove 2
!status
```

Rules and instructions are appended to the system prompt on every request.
Changes take effect immediately — no restart needed.

## Adding Tools (Future)

Add tool functions to a `tools/` folder and register them in `conversation.py`.
Each tool is a Python function the agent can call:
- `tools/freshservice.py` — create/update tickets
- `tools/sharepoint.py`  — search KB / runbooks
- `tools/slack.py`       — post notifications
- `tools/email.py`       — send via Graph API
```
