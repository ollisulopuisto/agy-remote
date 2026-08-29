# Mailbox snippet for project AGENTS.md

Copy-paste block for the `AGENTS.md` of a repo where agy sessions should be able
to message each other. It only makes sense where `agy-remote` is supervising the
session (it exports `AGY_REMOTE_SESSION_ID` into every agy it spawns, which is
what signs the message).

```markdown
## Talking to other agy sessions

If you need another agy session to do something (or to know the result of
something), do not try to call it or share files ad hoc. Post to its mailbox:

    agy-msg <session-name> "your message"

- The session name is the other session's tmux name (for example `agy-remote-api`).
  Your own name is in the environment variable `AGY_REMOTE_SESSION_ID`.
- Messages are delivered to the other session's prompt queue and shown to the
  human there. A message is a request or a fact, at most 4 KB.
- If the command is not found, say so in your reply; do not fall back to
  `echo >> …/mailbox/…` unless the human has told you the exact path.
- Sending is a normal tool call: the human approves it on their phone. After
  they allow it once for the pair, steady-state messaging no longer prompts.
- Do not reply to a message with another message in a tight loop; if you find
  yourself alternating back and forth, stop and ask the human to decide.
```