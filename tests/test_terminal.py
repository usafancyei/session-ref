import os
import pty
import select
import signal
import sys
import termios
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from session_ref.cli import interactive
from session_ref.keys import START, END
from session_ref.picker import Picker
from session_ref.screen import RESET_FRAME
from session_ref.terminal import Terminal, end_child, run_pty


class ImmediatePicker:
    eof = True

    def __init__(self, selected):
        self.selected = selected

    def input(self, token):
        assert token.startswith(b"\r")
        return self.selected, token[1:]

    def render(self, size):
        return b""

    def close(self):
        pass


class TerminalTests(unittest.TestCase):
    def test_escape_before_utf8_preserves_draft_at_every_read_boundary(self):
        with patch("session_ref.picker.subprocess.Popen"), \
             patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
             patch("session_ref.terminal.termios.tcsetwinsize"), \
             patch("session_ref.terminal.write_all") as write:
            for text in ("中文", "é", "🙂"):
                payload = b"\x1b" + text.encode()
                for cut in range(len(payload) + 1):
                    with self.subTest(text=text, cut=cut):
                        picker = Picker("fixture")
                        terminal = Terminal(42, b"#", lambda: picker)
                        terminal.input(None)
                        write.reset_mock()
                        for chunk in (payload[:cut], payload[cut:]):
                            for token in terminal.keys.feed(chunk):
                                terminal.input(token)
                        draft = b"".join(c.args[1] for c in write.call_args_list if c.args[0] == 42)
                        self.assertIsNone(terminal.picker)
                        self.assertEqual(picker.query, "")
                        self.assertEqual(draft, ("#" + text).encode())

    def test_resume_values_and_passthrough_aliases(self):
        for source, args in (("claude", ["--resume", "doctor"]), ("claude", ["-r", "logs"]),
                             ("claude", ["--name", "update"]), ("agy", ["--conversation", "models"]),
                             ("agy", ["-r", "models"]), ("agy", ["-i", "update"])):
            self.assertTrue(interactive(source, args), (source, args))
        for args in (["--resume", "--print"], ["--resume=doctor", "-p"]):
            self.assertFalse(interactive("claude", args))
        self.assertFalse(interactive("codex", ["a", "task-id"]))

    def test_end_child_reaps_on_signal_race(self):
        with patch("session_ref.terminal.os.killpg", side_effect=PermissionError), \
             patch("session_ref.terminal.os.waitpid", return_value=(42, 256)):
            self.assertEqual(end_child(42, signal.SIGHUP), 256)

    def test_resize_during_draw_and_margin_reset_before_replay(self):
        with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
             patch("session_ref.terminal.termios.tcsetwinsize"), \
             patch("session_ref.terminal.write_all") as write:
            t = Terminal(8, b"#", Mock())
            def render_during_resize(_size):
                t.resize()
                return b"frame"

            t.picker = SimpleNamespace(close=Mock(), render=render_during_resize)
            t.dirty = True
            t.draw()
            self.assertTrue(t.dirty)
            t.screen.alternate = True
            t.saved_frame = b"\x1b[2;5rhello"
            t.dismiss()
            writes = b"".join(c.args[1] for c in write.call_args_list)
            self.assertIn(b"\x1b[r", RESET_FRAME)
            self.assertLess(writes.index(RESET_FRAME), writes.index(b"\x1b[2;5rhello"))

    def test_resize_fallback_processes_old_frame_before_full_repaint(self):
        for leaves_screen in (False, True):
            with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
                 patch("session_ref.terminal.termios.tcsetwinsize"), \
                 patch("session_ref.terminal.write_all") as write:
                t = Terminal(8, b"#", Mock())
                t.screen.alternate = True
                t.picker = Mock()
                data = b"shrunken\x1b[6n" + (b"\x1b[?1049lprimary" if leaves_screen else b"")
                t.output(data)
                t.dismiss()
                result = b"".join(c.args[1] for c in write.call_args_list)
                self.assertEqual(result.count(data), 1)
                self.assertEqual(b"\x1b[2J" in result[len(data):], not leaves_screen)
                self.assertFalse(t.deferred)

    def test_interactive_entry_points(self):
        for source, args in [("codex", ["resume", "--last"]), ("codex", ["-m", "exec"]),
                             ("claude", ["--resume", "abc"]), ("agy", ["-r", "abc"])]:
            self.assertTrue(interactive(source, args))
        for source, args in [("codex", ["-m", "foo", "exec"]), ("codex", ["update"]),
                             ("claude", ["-p", "foo"]), ("claude", ["--model=x", "update"]),
                             ("agy", ["--prompt=x"]), ("agy", ["acp"]), ("agy", ["--version"])]:
            self.assertFalse(interactive(source, args))

    def test_real_pty_routing_resize_and_exit_status(self):
        # Use a recording child in a private PTY; no real conversation is opened.
        program = """
import os, termios, tty
tty.setraw(0)
os.write(1, b'READY')
data = bytearray()
while (c := os.read(0, 1)) != b'\\x04':
    data += c
rows, cols = termios.tcgetwinsize(0)
os.write(1, f'<{rows}x{cols}>{data.hex()}'.encode())
raise SystemExit(23)
"""
        pid, fd = pty.fork()
        if not pid:
            choices = iter([b"codex-id", b"", b"agy-id"])
            code = run_pty([sys.executable, "-c", program], b"#",
                           lambda: ImmediatePicker(next(choices)))
            os._exit(code)
        reaped = False
        try:
            termios.tcsetwinsize(fd, (37, 101))
            captured = bytearray()
            sent = False
            until = time.monotonic() + 8
            while time.monotonic() < until:
                if not select.select([fd], [], [], 0.25)[0]:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                if b"READY" in captured and not sent:
                    termios.tcsetwinsize(fd, (42, 95))
                    os.write(fd, "前后".encode() + b"\x1b[D#\r@#\r" + START + b"#p" + END + b"#\r\x03\x04")
                    sent = True
            while time.monotonic() < until:
                result, status = os.waitpid(pid, os.WNOHANG)
                if result == pid:
                    reaped = True
                    break
                time.sleep(0.01)
            self.assertTrue(reaped, "PTY child did not exit")
            self.assertEqual(os.waitstatus_to_exitcode(status), 23)
            expected = "前后".encode() + b"\x1b[Dcodex-id@##pagy-id\x03"
            self.assertIn(b"<42x95>" + expected.hex().encode(), captured)
        finally:
            os.close(fd)
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
