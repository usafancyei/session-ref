import os
import re
import subprocess
import unittest
from unittest.mock import patch

from session_ref.catalog import record
from session_ref.keys import END, START
from session_ref.picker import Picker
from session_ref.screen import Screen
from session_ref.view import clip, shorten, width_of


class PickerTests(unittest.TestCase):
    def setUp(self):
        with patch("session_ref.picker.subprocess.Popen"):
            self.picker = Picker("fixture")
        self.identifier = "12345678-1234-1234-1234-123456789abc"
        rows = [record("codex", self.identifier, "修复部署", "/work/project", 12),
                record("agy", "second", "部署", "/work/project", 10)]
        self.picker.rows = {(r["source"], r["id"]): r for r in rows}
        self.picker.filter()

    def test_cleanup_can_reap_after_group_signal_denied(self):
        loader = self.picker.loader
        loader.poll.return_value = None
        with patch("session_ref.picker.os.killpg", side_effect=PermissionError):
            self.picker.close()
        loader.wait.assert_called_once_with(timeout=0.1)
        loader.stdout.close.assert_called_once()

    def test_legacy_option_keys_and_coalesced_cancel(self):
        p = self.picker
        p.edit("first second")
        self.assertIsNone(p.input(b"\x1b\x7f"))
        self.assertEqual(p.query, "first ")
        for token in (b"\x1bb", b"\x1bf", b"\x1bd"):
            self.assertIsNone(p.input(token))
            self.assertEqual(p.query, "first ")
        for second in (b"\r", b"\x1b", b"\t", b"\x03"):
            self.assertEqual(p.input(b"\x1b" + second), (None, b""))

    def test_kitty_shortcuts_event_types_and_locks(self):
        p = self.picker
        p.edit("abc")
        p.input(b"\x1b[117;69u")
        self.assertEqual(p.query, "")
        p.input(b"\x1b[1;65:1B")
        p.input(b"\x1b[1;65:3B")
        self.assertEqual(p.index, 1)
        p.render((24, 80))
        p.input(b"\x1b[5;129:1~")
        self.assertEqual(p.index, 0)
        self.assertEqual(p.input(b"\x1b[13;129u"), (self.identifier.encode(), b""))
        self.assertEqual(p.input(b"\x1b[99;5u"), (None, b""))
        self.assertEqual(p.input(b"\x1b[27;65u"), (None, b""))

    def test_toggle_while_loading_tracks_newest_until_user_navigates(self):
        p = self.picker
        p.input(b"\t")
        p.rows["codex", "new"] = record("codex", "new", "", p.project, 20)
        p.filter()
        self.assertEqual(p.matches[p.index]["id"], "new")
        p.input(b"\x1b[B")
        selected = p.matches[p.index]["id"]
        p.rows["agy", "newest"] = record("agy", "newest", "", p.project, 30)
        p.filter()
        self.assertEqual(p.matches[p.index]["id"], selected)

    def test_short_display_errors_and_local_home(self):
        p = self.picker
        p.pending.clear()
        p.errors["codex"] = "测试读取失败"
        p.edit("missing")
        for height in (2, 4, 6, 7):
            self.assertIn("测试读取失败".encode(), p.render((height, 80)))
        p.rows["codex", "home"] = record("codex", "home", "home", os.path.expanduser("~"), 50)
        p.edit("home")
        frame = p.render((24, 80))
        self.assertIn(b"~", frame)
        self.assertNotIn(os.path.expanduser("~").encode(), frame)
        self.assertEqual(clip("\u200babc", 0), "")
        self.assertEqual(shorten("wide e\u0301x", 4, tail=True), "...x")

    def test_replay_accepts_xterm_sgr0_without_repeating_queries(self):
        s = Screen()
        reset = subprocess.check_output(["tput", "-T", "xterm-256color", "sgr0"])
        s.observe(b"\x1b[?1049h" + reset + b"title\x1b[6n")
        self.assertEqual(s.replay(), reset + b"title")

    def test_search_paste_and_narrow_terminal(self):
        picker = self.picker
        for ch in "部署".encode():
            picker.input(bytes([ch]))
        picker.input(START)
        picker.input(b"\r\nPROJECT")
        picker.input(END)
        self.assertEqual(len(picker.matches), 2)
        frame = picker.render((12, 24)).decode()
        self.assertNotIn(self.identifier[:8], frame)
        self.assertNotIn(self.identifier[-8:], frame)
        self.assertIn("修复部署", frame)
        self.assertEqual(clip("a中文", 4), "a中")
        picker.input(b"\x1bOB")
        self.assertEqual(picker.input(b"\rtext"), (b"second", b"text"))
        picker.input(b"\x1bOA")
        self.assertEqual(picker.input(b"\r"), (self.identifier.encode(), b""))

    def test_switch_groups_without_losing_query_or_selected_session(self):
        p = self.picker
        p.project = "/workspace/console"
        p.pending.clear()
        p.rows.clear()
        for source, sid, name, t in [("codex", "s1", "console", 1), ("agy", "s2", "editor", 8),
                                     ("claude", "s3", "notes", 7), ("claude", "s4", "editor", 6)]:
            p.rows[source, sid] = record(source, sid, "中文测试", f"/workspace/{name}", t)
        p.edit("测试")
        self.assertEqual([r["id"] for r in p.matches], ["s1", "s2", "s3", "s4"])
        p.input(b"\x1b[B")
        p.input(b"\x1b[B")
        p.input(b"\t")
        self.assertEqual([r["id"] for r in p.matches], ["s1", "s2", "s4", "s3"])
        self.assertEqual(p.input(b"\r"), (b"s3", b""))
        self.assertEqual(p.query, "测试")
        with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}):
            frame = p.render((24, 110)).decode()
            self.assertIn("项目视图", frame)
            self.assertIn("当前项目 · console", frame)
            self.assertIn("\x1b[7m", frame)
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            for size in ((1, 1), (4, 12), (12, 24), (24, 80), (30, 150)):
                frame = p.render(size).decode()
                lines = re.split(r"\x1b\[\d+;1H\x1b\[2K", frame)[1:]
                self.assertEqual(len(lines), size[0])
                self.assertTrue(all(width_of(line) <= size[1] for line in lines))
                self.assertNotIn("\x1b[7m", frame)
                self.assertNotIn("s3", frame)
        p.input(b"\t")
        self.assertFalse(p.by_project)
        self.assertEqual(p.input(b"\r"), (b"s3", b""))

    def test_no_match_and_cancel_before_load(self):
        picker = self.picker
        picker.input(b"missing\r")
        self.assertEqual(picker.matches, [])
        self.assertIsNone(picker.input(b"\r"))
        self.assertEqual(picker.input(b"\x1b@"), (None, b"@"))
        picker.input(b"\x15")
        self.assertEqual(picker.input(b"\x1b[13u"), (self.identifier.encode(), b""))

    def test_recent_five_per_source_after_search(self):
        picker = self.picker
        picker.rows.clear()
        for source, count, offset in (("claude", 6, 2), ("codex", 8, 1), ("agy", 2, 0)):
            for i in range(count):
                row = record(source, f"{source}-{i}", "部署" if i < 7 else "其他", "", 3 * i + offset)
                picker.rows[source, row["id"]] = row
        picker.filter()
        self.assertEqual([r["id"] for r in picker.matches], [
            "codex-7", "codex-6", "claude-5", "codex-5", "claude-4", "codex-4",
            "claude-3", "codex-3", "claude-2", "claude-1", "agy-1", "agy-0",
        ])
        picker.edit("Codex 部署")
        self.assertEqual([r["id"] for r in picker.matches],
                         ["codex-6", "codex-5", "codex-4", "codex-3", "codex-2"])
        picker.edit("codex-0")
        self.assertEqual(picker.input(b"\r"), (b"codex-0", b""))

    def test_screen_replays_drawing_only_across_read_boundaries(self):
        screen = Screen()
        chunks = [b"primary\x1b[?104", b"9h\x1b[?25l\x1b[2Jdraft",
                  b"\x1b]52;c;dummy\x07\x1b[6n\x1b[2;4Hvisible"]
        for chunk in chunks:
            screen.observe(chunk)
        self.assertTrue(screen.alternate)
        self.assertFalse(screen.cursor_visible)
        self.assertEqual(screen.replay(), b"\x1b[2Jdraft\x1b[2;4Hvisible")
        screen.observe(b"\x1b[?1049l")
        self.assertFalse(screen.alternate)
        self.assertIsNone(screen.replay())
