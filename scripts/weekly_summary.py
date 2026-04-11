"""Weekly health progress summary — runs on a GitHub Actions cron.

Starts a session against a pre-configured Anthropic Managed Agent, which already
has the model, system prompt, and MCP tools (Notion, Coupler.io, Withings, etc.)
wired up via its environment and vault. We just send the weekly prompt, stream
the reply to the Actions log, and archive the session when done.
"""

import os
import sys

import anthropic

BETA = "managed-agents-2026-04-01"


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> int:
    api_key = require_env("ANTHROPIC_API_KEY")
    agent_id = require_env("AGENT_ID")
    environment_id = require_env("ENVIRONMENT_ID")
    vault_id = require_env("VAULT_ID")

    client = anthropic.Anthropic(api_key=api_key)

    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        vault_ids=[vault_id],
        title="Weekly Progress Summary",
        betas=[BETA],
    )
    print(f"session: {session.id}", flush=True)

    client.beta.sessions.events.send(
        session_id=session.id,
        events=[
            {
                "type": "user.message",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Generate this week's progress summary: fetch my latest "
                            "weight and body composition from Withings, pull any "
                            "MacroFactor data from Google Sheets, compare to last week, "
                            "and log a structured summary to my Notion health tracker."
                        ),
                    }
                ],
            }
        ],
        betas=[BETA],
    )

    try:
        with client.beta.sessions.events.stream(
            session_id=session.id,
            betas=[BETA],
        ) as stream:
            for event in stream:
                event_type = getattr(event, "type", None)
                if event_type == "agent.message":
                    for block in event.content:
                        if block.type == "text":
                            print(block.text, end="", flush=True)
                elif event_type == "session.status_terminated":
                    break
        print()  # trailing newline after streamed output
    finally:
        client.beta.sessions.archive(session.id, betas=[BETA])
        print(f"archived session {session.id}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
