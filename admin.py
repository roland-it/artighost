"""
Admin command handler.

Commands:
  !rule add <text>          Add a rule (starts in training mode)
  !rule list                List all rules with mode
  !rule remove <id>         Remove rule by ID
  !rule graduate <id>       Promote rule from training → live
  !instruct add <text>      Add a global instruction
  !instruct list            List all instructions
  !instruct remove <n>      Remove instruction by number
  !status                   Show config summary
"""

import logging
from config import load_config, save_config
from vectorstore import add_rule, list_rules, delete_rule, graduate_rule

log = logging.getLogger(__name__)


def handle_admin_command(text: str) -> str:
    parts = text.strip().split(None, 2)
    if not parts:
        return _help()

    cmd = parts[0].lower()

    if cmd == "status":
        return _status()
    if cmd == "rule":
        return _handle_rules(parts)
    if cmd == "instruct":
        return _handle_instructions(parts)

    return _help()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _handle_rules(parts: list) -> str:
    if len(parts) < 2:
        return _help()

    sub = parts[1].lower()

    if sub == "list":
        rules = list_rules()
        if not rules:
            return "No rules defined yet."
        lines = []
        for r in rules:
            mode_icon = "🔴" if r.get("mode") == "live" else "🎓"
            lines.append(f"{mode_icon} `{r['id']}` — {r.get('pattern', '?')}")
        return "*Rules:*\n" + "\n".join(lines) + "\n\n🎓 = training  🔴 = live"

    if sub == "add":
        if len(parts) < 3 or not parts[2].strip():
            return "Usage: `!rule add <text>`"
        text = parts[2].strip()
        rule_id = add_rule(pattern=text, action="agent_decision", note=text, mode="training")
        return f"🎓 Rule added (training) `{rule_id}`: _{text}_"

    if sub == "remove":
        if len(parts) < 3:
            return "Usage: `!rule remove <id>`"
        rule_id = parts[2].strip()
        if delete_rule(rule_id):
            return f"Rule `{rule_id}` removed."
        return f"Could not find rule `{rule_id}`. Use `!rule list` to see current rules."

    if sub == "graduate":
        if len(parts) < 3:
            return "Usage: `!rule graduate <id>`"
        rule_id = parts[2].strip()
        if graduate_rule(rule_id):
            return f"🔴 Rule `{rule_id}` graduated to *live*. Alerts for this pattern will now go to the live channel."
        return f"Could not graduate rule `{rule_id}`. Use `!rule list` to verify the ID."

    return _help()


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

def _handle_instructions(parts: list) -> str:
    if len(parts) < 2:
        return _help()

    sub = parts[1].lower()
    config = load_config()
    items = config.setdefault("instructions", [])

    if sub == "list":
        if not items:
            return "No instructions defined yet."
        return "*Instructions:*\n" + "\n".join(f"{i+1}. {item}" for i, item in enumerate(items))

    if sub == "add":
        if len(parts) < 3 or not parts[2].strip():
            return "Usage: `!instruct add <text>`"
        item = parts[2].strip()
        items.append(item)
        save_config(config)
        return f"Instruction added ({len(items)} total): _{item}_"

    if sub == "remove":
        if len(parts) < 3:
            return "Usage: `!instruct remove <number>`"
        try:
            n = int(parts[2]) - 1
            removed = items.pop(n)
            save_config(config)
            return f"Instruction removed: _{removed}_"
        except (ValueError, IndexError):
            return "Invalid number. Use `!instruct list` to see current instructions."

    return _help()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _status() -> str:
    rules = list_rules()
    training = [r for r in rules if r.get("mode") == "training"]
    live = [r for r in rules if r.get("mode") == "live"]
    config = load_config()
    instructions = config.get("instructions", [])
    return (
        f"*Artighost Status*\n"
        f"🎓 Training rules: {len(training)}\n"
        f"🔴 Live rules: {len(live)}\n"
        f"📋 Global instructions: {len(instructions)}\n"
        f"Use `!rule list` or `!instruct list` to see details."
    )


def _help() -> str:
    return (
        "*Admin Commands*\n"
        "`!rule add <text>` — add a behavior rule (starts in training)\n"
        "`!rule list` — list all rules with mode\n"
        "`!rule remove <id>` — remove a rule\n"
        "`!rule graduate <id>` — promote rule from training → live\n"
        "`!instruct add <text>` — add a global instruction (always included)\n"
        "`!instruct list` — list all instructions\n"
        "`!instruct remove <n>` — remove an instruction\n"
        "`!status` — show summary"
    )
