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
from session_ref.picker import Picker, SwitchCommand
from session_ref.screen import LIMIT, RESET_FRAME, Screen
from session_ref.terminal import Child, Terminal, Workspace, end_child, run_pty


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
    def test_cycle_skips_background_eof_and_stays_inside_open_pickers(self):
        for opener in (b"#", b" \\"):
            with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
                 patch("session_ref.terminal.termios.tcsetwinsize"), patch("session_ref.terminal.write_all"), \
                 patch("session_ref.terminal.spawn", return_value=Child(1, 7, Screen())):
                picker = Mock()
                picker.input.return_value = None
                w = Workspace("agy", [], None, None, b"#", lambda: picker)
                w.switch()
                self.assertFalse(w.cycle_order)
                codex = Child(2, 8, Screen())
                w.children = {"codex": codex, "claude": Child(3, 9, Screen(), eof=True), "agy": w.active}
                w.switch()
                self.assertIs(w.active, codex)
                for token in w.terminal.keys.feed(b"\x1f"):
                    w.input(token)
                self.assertIs(w.active, w.children["agy"])
                for token in w.terminal.keys.feed(opener + b"\x1c\x1b[35::92;5u\x1f\x1b[35::47;5u"):
                    w.input(token)
                self.assertIsNotNone(w.terminal.picker)
                self.assertFalse(w.cycle_order)
                self.assertIs(w.active, w.children["agy"])
                self.assertFalse(w.active.input_queue)

    def test_switch_controls_never_reach_the_original_draft(self):
        prefix = b" \\"
        events = (b"\x1b[I\x1b[O\x1b[<64;9;1M\x1b[Mabc\x1b[A\x1bOB\x1b[1;5D"
                  b"\x1b[57364u\t\x15c\x1b[117;5ucla\x7f")
        with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
             patch("session_ref.terminal.termios.tcsetwinsize"), patch("session_ref.terminal.write_all"):
            for cut in range(len(events) + 1):
                literal = bytearray()
                t = Terminal(8, b" #", Mock(), Mock(), literal.extend)
                for chunk in (prefix + b"cl", events[:cut], events[cut:]):
                    for token in t.keys.feed(chunk):
                        t.input(token)
                self.assertEqual(t.picker.name, "cl")
                for token in t.keys.feed(b"z"):
                    t.input(token)
                self.assertIsNone(t.picker)
                self.assertEqual(literal, prefix + b"clz")
            for cancel in (b"\x1b\r", b"\x1b\r\n", b"\x1b\n", b"\x1b[27u\x1b[13u"):
                for cut in range(len(cancel) + 1):
                    literal = bytearray()
                    t = Terminal(8, b" #", Mock(), Mock(), literal.extend)
                    for chunk in (prefix + b"cl", cancel[:cut], cancel[cut:], b"ok\r"):
                        for token in t.keys.feed(chunk):
                            t.input(token)
                    self.assertIsNone(t.picker)
                    self.assertEqual(literal, b"ok\r")
            text = "中文".encode()
            for cut in range(len(text) + 1):
                literal = bytearray()
                t = Terminal(8, b" #", Mock(), Mock(), literal.extend)
                for chunk in (prefix, b"\x1b" + text[:cut], text[cut:]):
                    for token in t.keys.feed(chunk):
                        t.input(token)
                self.assertIsNone(t.picker)
                self.assertEqual(literal, text)

    def test_detach_pops_keyboard_on_the_screen_that_pushed_it(self):
        enter, leave = b"\x1b[?1049h", b"\x1b[?1049l"
        pop = b"\x1b[<1u"
        for alternate in (False, True):
            with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
                 patch("session_ref.terminal.termios.tcsetwinsize"), \
                 patch("session_ref.terminal.write_all") as write:
                t = Terminal(8, b" #", Mock(), Mock())
                t.screen.observe(b"\x1b[>3u")
                if alternate:
                    t.screen.observe(enter + b"\x1b[>7u")
                t.show(SwitchCommand(b" \\"))
                write.reset_mock()
                t.attach(Child(2, 9, Screen()), Child(1, 8, t.screen))
                frame = b"".join(c.args[1] for c in write.call_args_list)
                before, after = frame.split(leave, 1)
                self.assertEqual(before.count(pop), int(alternate))
                self.assertEqual(after.count(pop), 1)

    def test_vpa_and_every_truncation_offset_of_drawing_tokens(self):
        for sequence in (b"\x1b[38;2;255;128;0m", b"\x1b(B", "界".encode()):
            for offset in range(len(sequence)):
                s = Screen()
                start = LIMIT // 2 + 1 - offset
                payload = b"x" * start + sequence
                s.observe(payload + b"y" * (LIMIT - len(payload)) + b"z")
                expected = (sequence if offset == 0 else b"") + b"y"
                self.assertTrue(s.primary.startswith(expected), (sequence, offset, s.primary[:24]))
                s.observe(b"\x1b[12drow")
                self.assertEqual(s.restore()[-8:], b"\x1b[12drow")

    def test_background_resize_invalidates_saved_frame(self):
        with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)) as size, \
             patch("session_ref.terminal.termios.tcsetwinsize"), \
             patch("session_ref.terminal.spawn", return_value=Child(1, 7, Screen())):
            w = Workspace("codex", [], None, None, b" #", Mock())
            hidden = Child(2, 8, Screen())
            hidden.screen.observe(b"\x1b[?1049hframe")
            w.children["agy"] = hidden
            size.return_value = (30, 90)
            w.resize()
            self.assertIsNone(hidden.screen.replay())
    def test_switch_name_editing_csi_keys_and_literal_fallback(self):
        prefix = b"\x1b[32u\x1b[92u"
        command = SwitchCommand(prefix)
        for token in (b"c", b"\x1b[108u", b"\x7f", b"l", b"aude", b"\x1b[101;1:3u"):
            self.assertIsNone(command.input(token))
        self.assertEqual(command.input(b"\x1b[13u"), "claude")
        command = SwitchCommand(prefix)
        self.assertIsNone(command.input(b"c"))
        self.assertEqual(command.input(b":/docs"), prefix + b"c:/docs")
        self.assertEqual(SwitchCommand(prefix).input(b"\x1b"), b"")

    def test_switched_screen_restores_modes_without_replaying_terminal_queries(self):
        drawing = b"\x1b[?1004h\x1b[>3u\x1b[?2004h"
        content = b"primary\x1b]52;c;private\x07\x1b[?1049halternate\x1b[6n"
        for cut in range(len(drawing + content) + 1):
            screen = Screen()
            screen.observe((drawing + content)[:cut])
            screen.observe((drawing + content)[cut:])
            self.assertTrue(screen.child_paste)
            self.assertEqual(screen.release(), b"\x1b[?1049l\x1b[<1u\x1b[?1004l")
            frame = screen.restore()
            self.assertIn(b"primary\x1b[>3u\x1b[?1049h", frame)
            self.assertIn(b"alternate\x1b[?1004h", frame)
            self.assertNotIn(b"private", frame)
            self.assertNotIn(b"\x1b[6n", frame)

    def test_exit_mid_paste_and_queries_stay_with_their_owner(self):
        with patch("session_ref.terminal.termios.tcgetwinsize", return_value=(24, 80)), \
             patch("session_ref.terminal.termios.tcsetwinsize"), \
             patch("session_ref.terminal.write_all") as write:
            terminal = Terminal(8, b" #", Mock(), Mock())
            old = Child(1, 8, terminal.screen)
            target = Child(2, 9, Screen())
            target.screen.observe(b"\x1b[>3u\x1b[?u\x1b[5n\x1b[6", target.input_queue.extend)
            target.screen.observe(b"n", target.input_queue.extend)
            self.assertEqual(target.input_queue, b"\x1b[?3u\x1b[0n")
            self.assertEqual(list(terminal.keys.feed(START + b"first")), [b"first"])
            terminal.attach(target, old)
            output = b"".join(call.args[1] for call in write.call_args_list)
            self.assertEqual(output.count(b"\x1b[6n"), 1)
            self.assertFalse(target.screen.queries)
            self.assertEqual(list(terminal.keys.feed(b" \\agy\r" + END[:3])), [])
            self.assertEqual(list(terminal.keys.feed(END[3:] + b"new")), [b"new"])

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
