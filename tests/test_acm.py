"""Tests for acm.py. Standard library only: `python -m unittest discover tests`.

The merge is tested twice: as a pure function, and through a real `git merge` of two
branches, because the claim being made is about what git does with the file, and a pure
test cannot see a missing .gitattributes line or a driver git never calls.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acm  # noqa: E402


def entry(id_: str, ts: str, kind: str = "note", branch: str = "main", **fields: str) -> dict:
    return {"id": id_, "ts": ts, "branch": branch, "type": kind, **fields}


def decision(id_: str, ts: str, topic: str, value: str, branch: str = "main") -> dict:
    return entry(id_, ts, "decision", branch, topic=topic, value=value)


class MergeFunction(unittest.TestCase):
    def test_union_sorted_by_time_with_no_duplicates(self) -> None:
        base = [entry("b1", "2026-01-01T00:00:00Z")]
        ours = [*base, entry("o1", "2026-01-03T00:00:00Z")]
        theirs = [*base, entry("t1", "2026-01-02T00:00:00Z")]
        merged = acm.merge(base, ours, theirs, "main")
        self.assertEqual([e["id"] for e in merged], ["b1", "t1", "o1"])

    def test_same_timestamp_orders_by_id(self) -> None:
        ts = "2026-01-02T00:00:00Z"
        merged = acm.merge([], [entry("zz", ts)], [entry("aa", ts)], "main")
        self.assertEqual([e["id"] for e in merged], ["aa", "zz"])

    def test_both_sides_deciding_differently_is_a_conflict(self) -> None:
        base = [decision("d0", "2026-01-01T00:00:00Z", "database", "none")]
        ours = [*base, decision("d1", "2026-01-02T00:00:00Z", "database", "postgres", "a")]
        theirs = [*base, decision("d2", "2026-01-03T00:00:00Z", "database", "sqlite", "b")]
        merged = acm.merge(base, ours, theirs, "main")

        conflicts = acm.unresolved_conflicts(merged)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual([s["value"] for s in conflicts[0]["sides"]], ["postgres", "sqlite"])
        # Latest still wins as the current value; the conflict is what makes it visible.
        self.assertEqual(acm.latest_decisions(merged)["database"]["value"], "sqlite")

    def test_only_one_side_changing_is_not_a_conflict(self) -> None:
        base = [decision("d0", "2026-01-01T00:00:00Z", "database", "postgres")]
        ours = [*base, decision("d1", "2026-01-02T00:00:00Z", "database", "sqlite")]
        theirs = [*base, entry("n1", "2026-01-03T00:00:00Z", text="unrelated")]
        self.assertEqual(acm.unresolved_conflicts(acm.merge(base, ours, theirs, "main")), [])

    def test_agreeing_independently_is_not_a_conflict(self) -> None:
        ours = [decision("d1", "2026-01-02T00:00:00Z", "database", "sqlite", "a")]
        theirs = [decision("d2", "2026-01-03T00:00:00Z", "database", "sqlite", "b")]
        self.assertEqual(acm.unresolved_conflicts(acm.merge([], ours, theirs, "main")), [])

    def test_merging_again_does_not_duplicate_the_conflict(self) -> None:
        ours = [decision("d1", "2026-01-02T00:00:00Z", "database", "postgres")]
        theirs = [decision("d2", "2026-01-03T00:00:00Z", "database", "sqlite")]
        once = acm.merge([], ours, theirs, "main")
        twice = acm.merge([], once, theirs, "main")
        self.assertEqual(len([e for e in twice if e["type"] == "conflict"]), 1)

    def test_a_later_decision_resolves_the_conflict(self) -> None:
        ours = [decision("d1", "2026-01-02T00:00:00Z", "database", "postgres")]
        theirs = [decision("d2", "2026-01-03T00:00:00Z", "database", "sqlite")]
        merged = acm.merge([], ours, theirs, "main")
        conflict_ts = acm.unresolved_conflicts(merged)[0]["ts"]
        # Same second as the merge, and an id that sorts before the conflict's: still closes it.
        merged.append(decision("0", conflict_ts, "database", "postgres"))
        self.assertEqual(acm.unresolved_conflicts(merged), [])

    def test_a_decision_before_the_conflict_does_not_resolve_it(self) -> None:
        ours = [decision("d1", "2026-01-02T00:00:00Z", "database", "postgres")]
        theirs = [decision("d2", "2026-01-03T00:00:00Z", "database", "sqlite")]
        merged = acm.merge([], ours, theirs, "main")
        merged.append(decision("d0", "2026-01-01T00:00:00Z", "database", "mysql"))
        self.assertEqual(len(acm.unresolved_conflicts(merged)), 1)

    def test_unparseable_lines_survive_a_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text('{"id": "a", "ts": "2026-01-01T00:00:00Z", "type": "note"}\n'
                            "not json at all\n", encoding="utf-8")
            entries = acm.read_entries(path)
            acm.write_entries(path, acm.merge([], entries, [], "main"))
            self.assertIn("not json at all", path.read_text(encoding="utf-8"))


class GitRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        acm.init(self.root)
        (self.root / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self.commit("initial")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = acm.git(*args, cwd=self.root)
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        return result

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def log_text(self) -> str:
        return (self.root / acm.LOG_REL).read_text(encoding="utf-8")


class RealGitMerge(GitRepoTest):
    def test_two_branches_merge_their_context(self) -> None:
        self.git("checkout", "-q", "-b", "feature-a")
        acm.append_entry(self.root, decision("a1", "2026-01-02T00:00:00Z", "database", "postgres", "feature-a"))
        acm.append_entry(self.root, entry("a2", "2026-01-04T00:00:00Z", text="JWT works", branch="feature-a"))
        self.commit("a")

        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "feature-b")
        acm.append_entry(self.root, decision("b1", "2026-01-03T00:00:00Z", "database", "sqlite", "feature-b"))
        acm.append_entry(self.root, entry("b2", "2026-01-05T00:00:00Z", text="Stripe needs webhooks", branch="feature-b"))
        self.commit("b")

        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "feature-a")                         # fast-forward
        merge = self.git("merge", "--no-edit", "feature-b", check=False)  # both sides changed

        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        text = self.log_text()
        self.assertNotIn("<<<<<<<", text)
        entries = acm.read_entries(self.root / acm.LOG_REL)
        ids = [e["id"] for e in entries if e["type"] != "conflict"]
        self.assertEqual(ids, ["a1", "b1", "a2", "b2"])
        self.assertEqual([c["topic"] for c in acm.unresolved_conflicts(entries)], ["database"])
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")

    def test_init_is_idempotent(self) -> None:
        self.assertEqual(acm.init(self.root), [])
        attributes = (self.root / ".gitattributes").read_text(encoding="utf-8")
        self.assertEqual(attributes.count(acm.ATTRIBUTES_LINE), 1)


class StopHook(GitRepoTest):
    def run_hook(self, event: str, **data: object) -> str:
        payload = {"cwd": str(self.root), "session_id": "s1", **data}
        out = io.StringIO()
        with redirect_stdout(out):
            if event == "session-start":
                acm.hook_session_start(payload)
            else:
                acm.hook_stop(payload)
        return out.getvalue()

    def test_session_start_prints_context(self) -> None:
        acm.append_entry(self.root, decision("d1", "2026-01-02T00:00:00Z", "database", "sqlite"))
        out = self.run_hook("session-start")
        self.assertIn("database: sqlite", out)
        self.assertIn("acm.py", out)

    def test_code_change_without_a_note_blocks_once(self) -> None:
        self.run_hook("session-start")
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")

        blocked = self.run_hook("stop")
        self.assertEqual(json.loads(blocked)["decision"], "block")
        # Claude stops again without writing anything: allowed, and it does not ask again.
        self.assertEqual(self.run_hook("stop", stop_hook_active=True), "")
        self.assertEqual(self.run_hook("stop"), "")

    def test_code_change_with_a_note_passes(self) -> None:
        self.run_hook("session-start")
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        acm.append_entry(self.root, entry("n1", "2026-01-02T00:00:00Z", text="why"))
        self.assertEqual(self.run_hook("stop"), "")

    def test_no_code_change_passes(self) -> None:
        self.run_hook("session-start")
        self.assertEqual(self.run_hook("stop"), "")

    def test_repository_not_opted_in_is_left_alone(self) -> None:
        (self.root / acm.LOG_REL).unlink()
        self.assertEqual(self.run_hook("session-start"), "")
        self.assertEqual(self.run_hook("stop"), "")


if __name__ == "__main__":
    unittest.main()
