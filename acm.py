"""agent-context-merger: agent notes that live in the branch and merge when the code merges.

The notes are one append-only file, `.context/log.jsonl`, committed like any other file.
Git hands that file to `merge-driver` instead of writing conflict markers into it, so a
`git merge` of two branches also merges what the two sessions on them learned.

Only what git cannot know goes in the log: decisions, findings, dead ends. Commits are
read from `git log` when context is rendered, never copied into the log.

    acm.py init                               opt a repository in
    acm.py note "text"                        record a finding or a dead end
    acm.py decide <topic> "<value>" --why ..  record a decision; a later one supersedes it
    acm.py context                            what a new session should know
    acm.py merge-driver %O %A %B %P           called by git, not by hand
    acm.py hook session-start|stop            called by Claude Code, not by hand

Standard library only, so the plugin installs with nothing but a Python on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_REL = Path(".context") / "log.jsonl"
DRIVER = "agent-context"
# eol=lf: the file is rewritten by the driver, so pin its line endings or a Windows clone
# and a Linux clone disagree about every line of it.
ATTRIBUTES_LINE = f"{LOG_REL.as_posix()} merge={DRIVER} eol=lf"
STATE_FILE = "acm-state.json"


# --- git -----------------------------------------------------------------------------

def git(*args: str, cwd: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def repo_root(cwd: Path | str) -> Path | None:
    result = git("rev-parse", "--show-toplevel", cwd=cwd)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def current_branch(cwd: Path | str) -> str:
    result = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    name = result.stdout.strip()
    # A repository with no commits yet has no HEAD to resolve, but it does have a branch.
    if result.returncode != 0 or name == "HEAD":
        name = git("symbolic-ref", "--short", "-q", "HEAD", cwd=cwd).stdout.strip()
    return name or "detached"


# --- the log -------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def order(entry: dict) -> tuple[str, str]:
    # Time first, id as the tie-break, so two machines sort the same entries identically.
    return (str(entry.get("ts", "")), str(entry.get("id", "")))


def read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("not an object")
        except ValueError:
            # Kept, not dropped. A merge that silently discards a line it cannot parse
            # loses someone's note at exactly the moment nobody is looking.
            entry = {"id": "raw-" + hashlib.sha1(line.encode()).hexdigest()[:10],
                     "ts": "", "type": "unparsed", "_raw": line}
        entries.append(entry)
    return entries


def write_entries(path: Path, entries: list[dict]) -> None:
    lines = [e["_raw"] if "_raw" in e else json.dumps(e, ensure_ascii=False) for e in entries]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")


def append_entry(root: Path, entry: dict) -> dict:
    path = root / LOG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def new_entry(root: Path, kind: str, **fields: str) -> dict:
    # Random ids, not sequential ones. Two branches counting from the same base would both
    # mint the same next number for different notes.
    return {"id": secrets.token_hex(6), "ts": now(), "branch": current_branch(root),
            "type": kind, **{k: v for k, v in fields.items() if v}}


def latest_decisions(entries: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for entry in sorted(entries, key=order):
        if entry.get("type") == "decision":
            latest[entry["topic"]] = entry
    return latest


def unresolved_conflicts(entries: list[dict]) -> list[dict]:
    # A conflict stays open until a decision on its topic is recorded at or after it, other
    # than the two that caused it. "At", because timestamps are whole seconds and a decision
    # made in the same second as the merge must still count.
    decisions = [e for e in entries if e.get("type") == "decision"]
    open_ = []
    for c in sorted(entries, key=order):
        if c.get("type") != "conflict":
            continue
        caused = {s.get("id") for s in c.get("sides", [])}
        if not any(d["topic"] == c["topic"] and d["id"] not in caused
                   and str(d.get("ts", "")) >= str(c.get("ts", "")) for d in decisions):
            open_.append(c)
    return open_


# --- the merge -----------------------------------------------------------------------

def merge(base: list[dict], ours: list[dict], theirs: list[dict], branch: str) -> list[dict]:
    """Union both sides by id, sort by time, and record each real disagreement.

    Ordering is resolved automatically. A disagreement is not: if both branches changed
    the same topic to different values since the merge base, the later one is still the
    current decision, but a conflict entry says so out loud, and stays open until someone
    decides again. Latest-wins without the entry would hide that there was a choice.
    """
    merged: dict[str, dict] = {}
    for entry in [*base, *theirs, *ours]:
        merged[entry["id"]] = entry

    was, mine, other = latest_decisions(base), latest_decisions(ours), latest_decisions(theirs)
    for topic in sorted(set(mine) & set(other)):
        o, t, b = mine[topic], other[topic], was.get(topic)
        changed_both = all(b is None or side["id"] != b["id"] for side in (o, t))
        if not changed_both or o.get("value") == t.get("value"):
            continue
        # Deterministic, so merging the same pair again, or merging the merge onward,
        # does not add a second entry for the same disagreement.
        pair = "".join(sorted([o["id"], t["id"]]))
        conflict_id = "conflict-" + hashlib.sha1(pair.encode()).hexdigest()[:10]
        if conflict_id in merged:
            continue
        sides = sorted([o, t], key=order)
        merged[conflict_id] = {
            "id": conflict_id, "ts": now(), "branch": branch, "type": "conflict",
            "topic": topic,
            "sides": [{k: s.get(k) for k in ("value", "branch", "ts", "id", "why") if s.get(k)}
                      for s in sides],
        }
    return sorted(merged.values(), key=order)


def run_merge_driver(base: str, ours: str, theirs: str) -> int:
    # Git's contract: read %O %A %B, write the result over %A, exit 0 for a clean merge.
    # Any failure exits non-zero, which git reports as a conflict rather than a success.
    ours_path = Path(ours)
    branch = current_branch(Path.cwd())
    result = merge(read_entries(Path(base)), read_entries(ours_path),
                   read_entries(Path(theirs)), branch)
    write_entries(ours_path, result)
    return 0


# --- setup ---------------------------------------------------------------------------

def driver_command() -> str:
    python = Path(sys.executable).as_posix()
    script = Path(__file__).resolve().as_posix()
    return f'"{python}" "{script}" merge-driver %O %A %B %P'


def ensure_merge_driver(root: Path) -> bool:
    # Lives in .git/config, which is never committed, so every clone needs it once. Kept
    # current on every session start too, because a plugin update moves the script path.
    wanted = driver_command()
    if git("config", "--get", f"merge.{DRIVER}.driver", cwd=root).stdout.strip() == wanted:
        return False
    git("config", f"merge.{DRIVER}.name", "agent context log", cwd=root)
    git("config", f"merge.{DRIVER}.driver", wanted, cwd=root)
    return True


def init(root: Path) -> list[str]:
    done = []
    log = root / LOG_REL
    if not log.exists():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        done.append(f"created {LOG_REL.as_posix()}")

    attributes = root / ".gitattributes"
    existing = attributes.read_text(encoding="utf-8") if attributes.exists() else ""
    if ATTRIBUTES_LINE not in existing.splitlines():
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with attributes.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{prefix}{ATTRIBUTES_LINE}\n")
        done.append("added the merge rule to .gitattributes")

    if ensure_merge_driver(root):
        done.append("registered the merge driver in .git/config")
    return done


# --- rendering -----------------------------------------------------------------------

def render_context(root: Path, notes: int = 10, commits: int = 5) -> str:
    entries = read_entries(root / LOG_REL)
    script = Path(__file__).resolve().as_posix()
    out = [f"## Agent context — branch {current_branch(root)}  ({LOG_REL.as_posix()})"]

    conflicts = unresolved_conflicts(entries)
    if conflicts:
        out.append("\n### Unresolved conflicts — raise these with the user before relying on them")
        for c in conflicts:
            sides = "  vs  ".join(f'"{s.get("value")}" ({s.get("branch")}, {s.get("ts")})'
                                  for s in c.get("sides", []))
            out.append(f"- {c['topic']}: {sides}")

    decisions = latest_decisions(entries)
    if decisions:
        out.append("\n### Current decisions")
        for topic, d in sorted(decisions.items()):
            why = f" — {d['why']}" if d.get("why") else ""
            out.append(f"- {topic}: {d.get('value')}{why}  [{d.get('branch')}, {d.get('ts')}]")

    recent = [e for e in sorted(entries, key=order) if e.get("type") == "note"][-notes:]
    if recent:
        out.append(f"\n### Recent notes (last {len(recent)})")
        out.extend(f"- [{e.get('ts', '')[:10]} {e.get('branch')}] {e.get('text')}" for e in recent)

    log = git("log", "--oneline", f"-{commits}", cwd=root)
    if log.returncode == 0 and log.stdout.strip():
        out.append(f"\n### Recent commits\n{log.stdout.rstrip()}")

    out.append(
        "\n### Recording — this log is how the next session, on any branch, learns what you did\n"
        f'- Finding or dead end: python "{script}" note "<one line>"\n'
        f'- Decision: python "{script}" decide <topic> "<choice>" --why "<reason>"\n'
        "  Reuse an existing topic name to supersede a decision; that is what lets a merge "
        "notice two branches deciding the same thing differently.\n"
        "- Record what git cannot show: why, what was rejected, what did not work. "
        "Not what changed.")
    return "\n".join(out)


# --- Claude Code hooks ---------------------------------------------------------------

def read_hook_input() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def fingerprint(root: Path) -> str:
    # What the working tree looks like, excluding the log itself, so "code changed but no
    # note was written" can be asked without knowing which tools ran.
    exclude = ["--", ".", f":(exclude){LOG_REL.parent.as_posix()}"]
    parts = [git("rev-parse", "HEAD", cwd=root).stdout,
             git("status", "--porcelain", *exclude, cwd=root).stdout,
             git("diff", *exclude, cwd=root).stdout,
             git("diff", "--cached", *exclude, cwd=root).stdout]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def state_path(root: Path) -> Path:
    # Inside the git dir: per worktree, never committed, gone with the clone.
    git_dir = git("rev-parse", "--absolute-git-dir", cwd=root).stdout.strip()
    return Path(git_dir) / STATE_FILE


def load_state(root: Path) -> dict:
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_checkpoint(root: Path, session: str) -> None:
    state = load_state(root)
    state[session] = {"fp": fingerprint(root), "n": len(read_entries(root / LOG_REL))}
    state_path(root).write_text(json.dumps(state), encoding="utf-8")


def hook_session_start(data: dict) -> int:
    root = repo_root(data.get("cwd") or Path.cwd())
    if root is None or not (root / LOG_REL).exists():
        return 0  # not opted in; touch nothing
    ensure_merge_driver(root)
    save_checkpoint(root, str(data.get("session_id", "default")))
    print(render_context(root))
    return 0


def hook_stop(data: dict) -> int:
    root = repo_root(data.get("cwd") or Path.cwd())
    if root is None or not (root / LOG_REL).exists():
        return 0
    session = str(data.get("session_id", "default"))
    before = load_state(root).get(session)

    # Second stop in a row: we already asked once this turn. Claude may have judged there
    # was nothing worth recording, and asking again would loop.
    if data.get("stop_hook_active") or before is None:
        save_checkpoint(root, session)
        return 0

    code_changed = fingerprint(root) != before["fp"]
    noted = len(read_entries(root / LOG_REL)) != before["n"]
    if code_changed and not noted:
        script = Path(__file__).resolve().as_posix()
        print(json.dumps({"decision": "block", "reason": (
            "The code changed this turn and nothing was added to the agent context log. "
            "If you made a decision, rejected an alternative, or found something that did "
            "not work, record it now so sessions on other branches inherit it: "
            f'python "{script}" decide <topic> "<choice>" --why "<reason>"  or  '
            f'python "{script}" note "<one line>". '
            "If nothing is worth recording, just stop again.")}))
        return 0

    save_checkpoint(root, session)
    return 0


# --- CLI -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("note")
    p.add_argument("text")
    p = sub.add_parser("decide")
    p.add_argument("topic")
    p.add_argument("value")
    p.add_argument("--why", default="")
    p = sub.add_parser("context")
    p.add_argument("--notes", type=int, default=10)
    p = sub.add_parser("merge-driver")
    p.add_argument("base")
    p.add_argument("ours")
    p.add_argument("theirs")
    p.add_argument("path", nargs="?", default="")  # %P, unused: one file, one rule
    p = sub.add_parser("hook")
    p.add_argument("event", choices=["session-start", "stop"])
    args = parser.parse_args(argv)
    # Hook output is read by Claude Code as UTF-8; a Windows console default would
    # turn every non-ASCII character in a note into a replacement glyph.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.command == "merge-driver":
        return run_merge_driver(args.base, args.ours, args.theirs)
    if args.command == "hook":
        data = read_hook_input()
        return hook_session_start(data) if args.event == "session-start" else hook_stop(data)

    root = repo_root(Path.cwd())
    if root is None:
        print("not inside a git repository", file=sys.stderr)
        return 1
    if args.command == "init":
        done = init(root)
        print("\n".join(done) if done else "already set up")
        return 0
    if not (root / LOG_REL).exists():
        print(f"no {LOG_REL.as_posix()} here; run `acm.py init` first", file=sys.stderr)
        return 1
    if args.command == "note":
        append_entry(root, new_entry(root, "note", text=args.text))
    elif args.command == "decide":
        append_entry(root, new_entry(root, "decision", topic=args.topic.strip().lower(),
                                     value=args.value, why=args.why))
    elif args.command == "context":
        print(render_context(root, notes=args.notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
