# agent-context-merger

Agent context that lives in the branch and merges when the code merges: a git merge driver
for AI session notes, with a Claude Code plugin.

## The problem

Two coding-agent sessions work on two branches. Each learns things: a library that is
broken, a decision made, an approach that failed. When the branches merge, git combines the
code, but what the sessions learned stays behind in two separate conversations.

## How it works

- Notes live in `.context/log.jsonl`, committed with the code. One line per note.
- Only what git cannot show goes in: decisions, findings, dead ends. Commits are read from
  `git log`, never copied.
- `.gitattributes` tells git to merge that file with `acm.py` instead of writing conflict
  markers. Both branches' notes are kept, sorted by time.
- If both branches **decided the same topic differently**, the later decision becomes
  current *and* a conflict entry is added, so the next session asks you instead of
  silently picking one.
- The Claude Code plugin loads the notes at the start of every session, and nudges Claude
  once if a turn changed code without recording anything.

## Try it

```bash
python acm.py init                                        # in your project
python acm.py decide database sqlite --why "no server"    # record a decision
python acm.py note "pandas 2.2 breaks the loader; pinned 2.1"
python acm.py context                                     # what a new session sees
```

Load the plugin into Claude Code for a session with `claude --plugin-dir <path to this repo>`.

Standard library only, Python 3.10+. Tests: `python -m unittest discover tests`.

## Licence

MIT
