# bloom

Local-first personal assistant. The application process, persistence, channels,
and task execution live on the laptop; model and connector calls are the only
remote calls.

## First slice

This repository starts from the hackathon plan's first must-ship item:

- OpenAI Responses API provider behind a provider seam
- local CLI channel for a fast development loop
- explicit approval gate for side-effecting tools
- provider and conversation tests which make no network calls
- BlueBubbles webhook adapter and local SQLite task/thread state
- in-process OpenAI Agents SDK + local Playwright task runner (optional extra)

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# add OPENAI_API_KEY, then inspect the account's actual model IDs
.venv/bin/python cli.py models
```

Put a model ID returned by `models` in `BLOOM_MODEL`; it is intentionally not
hard-coded. Then run:

```sh
.venv/bin/python cli.py chat
```

For browser tasks install the local task runtime (there is no hosted sandbox):

```sh
.venv/bin/pip install -e '.[tasks]'
.venv/bin/playwright install chromium
```

For Gmail and Google Calendar, install the connector runtime and set
`COMPOSIO_API_KEY`. At startup bloom resolves each toolkit's current version,
and every write-shaped tool is stopped behind an iMessage approval code.

```sh
.venv/bin/pip install -e '.[connectors]'
```

To enable iMessage, install and configure BlueBubbles on this Mac, set
`BLUEBUBBLES_URL` and `BLUEBUBBLES_PASSWORD` in `.env`, then register
`http://127.0.0.1:8787/bluebubbles/webhook` as its **New Messages** webhook.
Start bloom with `.venv/bin/python cli.py serve-imessage`. If BlueBubbles sends
webhooks through a public tunnel, also set `BLUEBUBBLES_WEBHOOK_TOKEN` and
configure its matching request header at the proxy.

For proactive events, run `.venv/bin/python cli.py serve-events` in a second
terminal and forward your Composio trigger listener to
`http://127.0.0.1:8788/events`. The listener accepts
`{"user_id":"imessage:+15551234567","text":"…"}`; it resolves the user’s
last iMessage chat locally and hard-limits unsolicited messages to three a day.

Run the offline test suite with `python -m unittest discover -s tests -v`.

The `voice/` boundary uses Saaras v3 in `codemix` mode for audio input and
Bulbul v3 for short spoken replies. It deliberately leaves voice attachment
transport at the channel boundary, so a future channel can send the same local
audio asset without changing ASR, language policy, or agent behavior.
