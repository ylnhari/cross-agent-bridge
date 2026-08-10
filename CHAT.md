# Agent Chat v2

V2 is deliberately a small talking app, not a workflow engine. Messages are
free-form text. Either agent can ask questions, answer, challenge assumptions,
share evidence, propose a plan, or request clarification.

Only the envelope is structured:

- room
- sender and recipient
- text
- optional reply-to message ID
- optional retry key
- acknowledgement timestamp

## Commands

```powershell
python chat.py --db .local/mycard-chat.sqlite3 init --room mycard-benefits

python chat.py --db .local/mycard-chat.sqlite3 send `
  --room mycard-benefits --from claude --to codex `
  --text "Please check whether these two records describe one benefit or two."

python chat.py --db .local/mycard-chat.sqlite3 receive `
  --room mycard-benefits --as codex --wait 300

python chat.py --db .local/mycard-chat.sqlite3 sync `
  --room mycard-benefits --as codex --with claude --wait 30

python chat.py --db .local/mycard-chat.sqlite3 ack `
  --room mycard-benefits --as codex --id 1

python chat.py --db .local/mycard-chat.sqlite3 history `
  --room mycard-benefits --limit 20
```

For multiline text in PowerShell, use a variable:

```powershell
$message = @'
I found two plausible interpretations.
Which one matches your intended product behavior, and why?
'@
python chat.py --db .local/mycard-chat.sqlite3 send --room mycard-benefits --from codex --to claude --text $message
```

`receive` returns the oldest unacknowledged message. It leaves the message
unacknowledged, so a crashed session receives it again. Acknowledge after the
agent has understood it, then answer with an optional `--reply-to`.

Use `sync` for active agent-to-agent operation. It waits once for an unread
message. If none arrives, it sends one natural-language `CHECK-IN` asking the
peer whether it is waiting, wants work to continue, or considers the run done.
It never sends another check-in while an earlier outgoing message is unread. A
later empty sync instead returns `peer_pending`, so the caller stops duplicate
polling and surfaces the pending message. This is not proof that the peer or
transport is dead: a desktop agent may simply be inside a long foreground turn.
If the peer acknowledged the check-in but did not answer, sync returns
`peer_reply_pending`.

On either pending status, do not enqueue another check-in. If the host already
has an authorized native way to wake the same session, use it once with the
pending message ID; otherwise report the condition visibly and let the user or
host wake the peer. Never create or reconnect a session merely to obtain a wake
path.

Every command requires an explicit `--db` and `--room`. New database files are
created only by `init`; a typo in a later command fails instead of creating a
second queue. Every JSON response includes the resolved database path and a
stable bridge ID. Each agent checks those values before starting work.

Waiting is performed by SQLite/Python, not by model generation. If a desktop
turn closes, the message remains stored but the app still needs a host, goal,
automation, or user wake-up.

## Boundaries outside the format

Conversation is flexible; authority is not. The user and target repository
instructions still decide scope. Claude remains orchestrator/reviewer and
Codex remains the sole repository writer for the current MyCard run. Neither
agent sends secrets, private data, raw transcripts, or unnecessarily large logs.
