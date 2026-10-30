---
name: agent-context
description: Record decisions, findings and dead ends in the branch's agent context log (.context/log.jsonl) so sessions on other branches inherit them when the branches merge. Use when a decision is made, an alternative is rejected, something turns out not to work, or the user asks to set up, read or resolve the agent context.
---

# Agent context log

Repositories opted in have `.context/log.jsonl`, committed with the code. When two
branches merge, a git merge driver combines both branches' entries in time order, so the
next session on the merged branch knows what both sessions learned.

## Setting a repository up

```bash
python "${CLAUDE_PLUGIN_ROOT}/acm.py" init
```

Creates the log, adds the merge rule to `.gitattributes` (commit both), and registers the
driver in `.git/config`. The session-start hook re-registers the driver in every clone.

If `${CLAUDE_PLUGIN_ROOT}` is not set in your shell, use the absolute path to `acm.py`
printed at the bottom of the session's *Agent context* block.

## What to record

Only what git cannot show. The diff already says *what* changed.

- **A decision:** `python "${CLAUDE_PLUGIN_ROOT}/acm.py" decide <topic> "<choice>" --why "<reason>"`.
  Reuse an existing topic name to change a decision. Topics are how a merge notices two
  branches choosing differently, so `database`, not `db-choice-for-feature-x`.
- **A finding or dead end:** `python "${CLAUDE_PLUGIN_ROOT}/acm.py" note "<one line>"`.
  For example "pandas 2.2 breaks the resample call in loader.py; pinned 2.1".

One line each. The log is read at every session start, so it has to stay short.

## Conflicts

If both branches decided the same topic differently, the merge keeps the later decision
as current and adds a `conflict` entry. The session-start context lists it under
*Unresolved conflicts*. Do not pick a side yourself: ask the user, then record their
answer with `decide` on the same topic, which closes the conflict.
