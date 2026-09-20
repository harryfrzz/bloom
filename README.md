# bloom

## Overview

bloom is a personal assistant that lives inside iMessage. You text it the way you
text a friend — in English, Hindi, Malayalam, or the romanised code-mix people
actually type — and it answers in the same language you used. It reads your mail,
watches for things you are waiting on, writes to your Reminders and Notes, sends
calendar invites you tap to accept, listens to voice notes and replies out loud,
looks at photos you send, remembers what you talked about weeks ago, and checks in
when you said you would do something and did not.

It runs as a daemon on your own Mac. There is no app to install and no website to
log into: the interface is the message thread you already have open.

## Problem Statement

Assistants live where you are not. Opening a tab, signing in, and typing a prompt
is a context switch, which is why most people only reach for one when they already
know they need it. The gap is worst for the things that would help most — someone
is waiting on a reply you forgot, a mail arrived that changes your day, you
promised a friend something on Tuesday and it is now Friday.

There is also a language problem. A large part of the world texts in romanised,
code-mixed Malayalam or Hindi — *"enikku naale oru meeting undu"*, *"bhai kal kya
plan hai"* — and assistants answer in English, or worse, in a script the person
does not read casually. Replying in Devanagari to someone typing Manglish is not
help, it is friction.

## Solution

bloom moves the assistant into the message layer and keeps it on your own machine.

Because it sits where your conversations already happen, it can see things no
cloud assistant can: which of your threads have someone waiting on a reply, what
you promised and to whom, what you have talked about before. Because it runs
locally, that access stays on your laptop rather than being uploaded somewhere.

And because it answers in the language you wrote in — Roman script in, Roman
script out — it reads like a person rather than a product.

## Features

* **Answers in your language.** English, Hindi and Malayalam, in native script or
  romanised, including Hinglish and Manglish. Speech is transcribed in Roman
  script so a voice note reads back the way you would have typed it. Any other
  language is named and politely declined rather than answered badly.
* **Voice notes both ways.** Send one and it is transcribed and answered; the
  model decides when a spoken reply fits and sends one back as a real voice
  bubble.
* **Sees pictures.** Send a photo and ask about it. Images are shrunk, uploaded
  once, and referenced by id so a long conversation does not pay for them again
  each turn.
* **Connected apps.** Gmail, Google Calendar, Notion and Slack through Composio's
  tool router. An app you have not connected offers a sign-in link in the chat
  instead of failing.
* **Apple Reminders, Notes and Calendar.** Tasks and notes are written directly on
  the Mac; events arrive as a calendar invite you tap to add, which needs no
  approval because the tap is the decision.
* **Watches.** "Tell me when mail from X arrives" records a standing watch. It
  learns what is already there first, so only genuinely new things are reported,
  and a fired watch is written up — *"the residency is restarting, they want you
  to reapply"* — rather than relayed raw.
* **Who is waiting on you.** Reads your threads for conversations where someone
  wrote and got nothing back, with short codes, OTPs, delivery notices and
  promotions filtered out. On the test machine that turned 179 threads into one
  person actually waiting.
* **Promises kept.** Scans the messages you sent other people for commitments you
  made, and asks how it went once one is overdue. At most three times, six hours
  apart, and never again once you say it is done.
* **Morning briefing.** One catch-up a day drawing on your calendar, reminders,
  who is waiting and what you owe people — written as a few lines, in your
  language, and silent on a morning with nothing to say.
* **Recall.** Conversations and watch results are embedded and searchable by
  meaning, so "what did we decide on Tuesday" works without matching words.
* **An approval gate.** Anything that changes the world outside bloom — sending
  mail, creating things, deleting — is held for a one-word confirmation first.

## Tech Stack

* **Frontend:** iMessage. There is no custom UI; the message thread is the
  interface, reached through a local BlueBubbles server.
* **Backend:** Python 3.14, standard library only for the service itself — an
  HTTP webhook receiver, a REST poller, and background threads for watches and
  the daily briefing. No web framework.
* **Database:** SQLite on the laptop for conversation state, approvals,
  identities, watches and commitments. Pinecone (serverless, cosine, 1536
  dimensions) for the searchable memory.
* **APIs / Services:** OpenAI Responses API (`gpt-5.6-luna`) and
  `text-embedding-3-small`; Composio Tool Router for connected apps; Sarvam AI for
  Indic speech-to-text and text-to-speech; BlueBubbles for iMessage; AppleScript
  for Reminders, Notes and Calendar.
* **Hosting / Deployment:** None. It runs as a `launchd` agent on the user's own
  Mac, which is the point rather than a limitation.
* **Other Tools:** `sips` for image resizing, `.ics` generation for calendar
  invites, `unittest` for the test suite.

## Codex / OpenAI Usage

**OpenAI models are the product, not just a build tool.** Every reply, every
decision about which tool to use, every watch write-up, briefing and check-in is a
Responses API call to `gpt-5.6-luna`. `text-embedding-3-small` powers recall.
Native tool calling drives the whole agent loop, and prompt caching keeps the cost
of a large tool surface manageable.

The model was also chosen by measurement rather than reputation. Five candidates
were benchmarked on this project's actual prompts, and `gpt-5.6-luna` won on both
accuracy and cost — 9/9 on the language matrix at 440 output tokens against
`gpt-5.4-mini`'s 8/9 at 967, for the same latency.

AI assistance was used throughout the build itself: designing the architecture,
writing the code and its 125 tests, and — most usefully — debugging. A long list of
real faults were found and fixed this way, including a broken tool-calling loop
that made every tool call fail, a context overflow caused by unbounded tool
results, an approval flow that looped forever, and speech synthesis that had never
worked because of an invalid default voice.

## Demo

### Live Demo

There is no hosted demo, and there cannot be one: bloom runs on a specific Mac,
signed into a specific iMessage account, with that person's mail and calendar
connected. Running it means running it yourself, which the section below covers.

### Demo / Pitch Video

_To be added._

## Screenshots

_To be added: the conversation thread showing a voice note answered in Manglish, a
calendar invite arriving as a tappable `.ics`, and a watch firing with a written-up
summary._

## How to Run Locally

bloom needs macOS, because it talks to iMessage through BlueBubbles and to
Reminders and Notes through AppleScript.

```bash
git clone https://github.com/harryfrzz/bloom.git
cd bloom
python3 -m venv .venv && .venv/bin/pip install -e ".[connectors,recall]"

cp .env.example .env     # then fill in OPENAI_API_KEY and BLOOM_MODEL
.venv/bin/python cli.py models   # lists the models your key can use

.venv/bin/python cli.py chat            # terminal conversation, no iMessage needed
.venv/bin/python cli.py serve-imessage  # the real thing
```

Before `serve-imessage`, install the [BlueBubbles server](https://bluebubbles.app)
and add a webhook pointing at `http://127.0.0.1:8787/bluebubbles/webhook`. Every
other key is optional and the matching feature simply stays switched off without
it: `COMPOSIO_API_KEY` for connected apps, `SARVAM_API_KEY` for voice,
`PINECONE_API_KEY` for recall. All settings are documented in `.env.example`.

```bash
.venv/bin/python -m unittest discover -s tests -t .   # 125 tests
```

## Additional Notes

**What is deliberately limited.** Indic language support is scoped to Hindi and
Malayalam. Romanised Tulu is occasionally mistaken for Manglish, which is a genuinely
hard distinction; Tamil, Telugu, Kannada and Bengali are reliably declined.
Reminders and Notes are written through AppleScript rather than sent as files,
because no file format reaches them reliably on Apple's side.

**What is honest about the architecture.** bloom is local-first, but not local-only.
Model calls, connected apps and the vector memory are remote by necessity. The
conversation database, message access, approvals and every decision about what to
do stay on the laptop. Recall was the first feature to send conversations off the
machine, and it is optional for that reason — the same design runs against a local
vector store.

**Known rough edges.** BlueBubbles' own message listener stalls and only recovers on
a restart, so bloom polls its REST API alongside the webhook and deduplicates by
message id; without that, replies silently stop. The Private API is not enabled,
which would require disabling SIP, so sends go through AppleScript and there are no
typing indicators or tapbacks. Promises with no stated deadline are recorded but
never chased, which is deliberate restraint rather than an oversight.

**Licence.** Released under the [PolyForm Noncommercial License
1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0): free to use,
modify and share for any noncommercial purpose, including personal projects,
research, education and nonprofits. Commercial use requires a separate licence
from the author. Note that this is a source-available licence rather than an
OSI-approved open source one, precisely because it restricts commercial use.

**What is next.** Group chats — an assistant that lives in a friend group rather
than a one-to-one thread. Reading an event poster from a photo straight into a
calendar invite. Drafting the replies for the people found waiting, rather than
only listing them.
