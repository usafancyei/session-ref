"""Exercise the real terminal selector through the normal zsh entry points."""

import concurrent.futures
import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path

from session_ref.cli import ROOT, SOURCES


CLI = r'''#!/usr/bin/env python3
import json, os, sys, tty
from pathlib import Path
name = Path(sys.argv[0]).name
if sys.argv[1:] in (["app-server"], ["acp"]):
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        if request["method"] == "initialize":
            result = {"agentCapabilities": {"sessionCapabilities": {"list": {}}}}
        elif request["method"] == "thread/list":
            identifier = "old-codex" if request["params"]["archived"] else "codex-ref"
            title = "Archived title" if request["params"]["archived"] else "Codex title"
            result = {"data": [{"id": identifier, "preview": title, "cwd": "/demo"}]}
        elif request["method"] == "session/list":
            result = {"sessions": [{"sessionId": "agy-ref", "cwd": "/demo"}]}
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
elif not sys.stdin.isatty() or "--version" in sys.argv:
    print(json.dumps(sys.argv[1:]))
    sys.exit(33)
else:
    tty.setraw(0)
    if name == "codex":
        os.write(1, b"\x1b[?1049h\x1b[H")
    os.write(1, b"READY")
    data = bytearray()
    while (c := os.read(0, 1)) != b"\x04":
        data += c
    os.write(1, b"GOT:" + data.hex().encode() + b":END")
    if name == "codex":
        os.write(1, b"\x1b[?1049l")
    sys.exit(23)
'''
SDK = '''from types import SimpleNamespace
def list_sessions():
    return [SimpleNamespace(session_id="claude-ref", custom_title="Claude title",
                            summary="", cwd="/demo", last_modified=1000)]
'''

SWITCH_CLI = r'''#!/usr/bin/env python3
import os, select, sys, tty
from pathlib import Path
name = Path(sys.argv[0]).name
root = Path(os.environ["SWITCH_FIXTURE"])
state = root / (name + ".launches")
with state.open("a") as log:
    print(os.getpid(), os.getcwd(), *sys.argv[1:], file=log)
tty.setraw(0)
if name == "codex":
    os.write(1, b"\x1b[?1049h\x1b[>1u")
os.write(1, f"<ready-{name}>".encode())
draft = bytearray()
probes = 0
while True:
    if name == "codex" and (root / "go").exists() and not (root / "done").exists():
        for _ in range(320):
            data = memoryview(b"x" * 4095 + b"\n")
            while data:
                data = data[os.write(1, data):]
        os.write(1, b"<background-done>")
        (root / "done").touch()
    if not select.select([0], [], [], 0.02)[0]:
        continue
    key = os.read(0, 1)
    if key == b"\x04":
        break
    if key == b"\x02":
        probes += 1
        os.write(1, f"<{name}-{probes}:{draft.hex()}>".encode())
    else:
        draft.extend(key)
        if key == b" ":
            os.write(1, b"<space-received>")
if name == "codex":
    os.write(1, b"\x1b[<u\x1b[?1049l")
os.write(1, f"<ended-{name}>".encode())
sys.exit(23)
'''


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-ref-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        shutil.copy2(ROOT / "session-ref", self.path)
        shutil.copytree(ROOT / "session_ref", self.path / "session_ref",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "shell", self.path / "shell")
        (self.path / "claude_agent_sdk.py").write_text(SDK)
        for source in SOURCES:
            path = self.path / source
            path.write_text(CLI)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.path) + os.pathsep + os.environ["PATH"])
        self.env.pop("SESSION_REF_TRIGGER", None)

    def command(self, source, *args):
        return ["zsh", "-fc", 'source "$1"; shift; "$@"', "fixture",
                str(self.path / "shell/session-ref.zsh"), source, *args]

    def test_piped_calls_preserve_separator_arguments_and_status(self):
        args = ["--", "--version", "a b", ""]
        for source in SOURCES:
            proc = subprocess.run(self.command(source, *args), env=self.env, input=b"",
                                  capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 33)
            self.assertEqual(json.loads(proc.stdout), args)

    def test_list_includes_archived_threads_and_untitled_agy(self):
        proc = subprocess.run(self.command("session-ref", "list"), env=self.env,
                              capture_output=True, text=True, check=True, timeout=5)
        rows = [line.split("\t") for line in proc.stdout.splitlines()]
        self.assertTrue(all(len(row) == 4 for row in rows))
        self.assertEqual({row[2] for row in rows}, {"Codex title", "Archived title", "Claude title", "（无标题）"})
        self.assertTrue(all(row[3] == "/demo" for row in rows))
        for sid in ("codex-ref", "old-codex", "agy-ref", "claude-ref"):
            self.assertNotIn(sid, proc.stdout)
        grouped = subprocess.run(self.command("session-ref", "list", "--by-project"), env=self.env,
                                 capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(grouped.stdout, "[/demo]\n" + proc.stdout)

    def session(self, source, selection, cancel=False, idle_control=False, after_escape=b""):
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (24, 80))
        proc = subprocess.Popen(self.command(source), stdin=slave, stdout=slave, stderr=slave,
                                env=self.env, start_new_session=True)
        os.close(slave)
        data = bytearray()
        opened = selected = continued = False
        resume_at = None
        try:
            end = time.monotonic() + 10
            while time.monotonic() < end and b":END" not in data:
                if idle_control and not continued and b"key-probe" in data:
                    # Start the idle interval only once the terminal has consumed
                    # the input. Keep draining PTY output throughout the interval.
                    if resume_at is None:
                        resume_at = time.monotonic() + 0.4
                    if time.monotonic() >= resume_at:
                        os.write(master, b"\x15" + selection.encode() + b"-ref\r@\x04")
                        continued = True
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                try:
                    piece = os.read(master, 8192)
                except OSError:
                    break
                if not piece:
                    break
                data.extend(piece)
                if b"READY" in data and not opened:
                    os.write(master, "前后".encode() + b"\x1b[D# #")
                    opened = True
                if "4 个会话".encode() in data and not selected:
                    if cancel:
                        os.write(master, b"no-such-session\x1b" + after_escape)
                    elif idle_control:
                        os.write(master, b"key-probe\x1b]")
                    else:
                        # Exercise both views through the actual terminal input path.
                        toggle = b"\t" if source == selection else b""
                        os.write(master, toggle + selection.encode() + b"-ref\r@\x04")
                    selected = True
                if cancel and selected and not continued and data.count(b"READY") == 2:
                    os.write(master, b"@\x1b[200~ #paste\x1b[201~\x04")
                    continued = True
            expected = "前后".encode() + b"\x1b[D# "
            expected += b"#" + after_escape + b"@ #paste" if cancel else selection.encode() + b"-ref@"
            self.assertIn(b"GOT:" + expected.hex().encode() + b":END", data)
            if source == "codex":
                self.assertEqual(data.count(b"READY"), 2)
                self.assertEqual(data.count(b"\x1b[?1049h"), 1)
            self.assertEqual(proc.wait(timeout=2), 23)
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)

    def test_nine_pairs_in_independent_terminals(self):
        # Open all three clients together so a selection sent to the wrong
        # terminal fails the assertion there as well.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            for offset in range(len(SOURCES)):
                futures = [executor.submit(self.session, source, SOURCES[(i + offset) % 3])
                           for i, source in enumerate(SOURCES)]
                for future in futures:
                    future.result()

    def test_cancel_restores_alternate_screen_and_paste(self):
        self.session("codex", "", cancel=True)

    def test_escape_and_chinese_in_one_terminal_read(self):
        self.session("codex", "", cancel=True, after_escape="中文".encode())

    def test_picker_recovers_on_control_string_idle_timeout(self):
        self.session("claude", "agy", idle_control=True)

    def switch_session(self, command):
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (24, 80))
        proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                env=self.env, start_new_session=True)
        os.close(slave)
        captured = bytearray()

        def expect(marker):
            deadline = time.monotonic() + 8
            while marker not in captured:
                if time.monotonic() >= deadline:
                    self.fail(f"missing {marker!r}, last output {captured[-160:]!r}")
                if select.select([master], [], [], 0.1)[0]:
                    chunk = os.read(master, 65536)
                    self.assertTrue(chunk, "unexpected end of terminal")
                    captured.extend(chunk)
            end = captured.index(marker) + len(marker)
            result = bytes(captured[:end])
            del captured[:end]
            return result

        probes = dict.fromkeys(SOURCES, 0)

        def draft(name, expected):
            probes[name] += 1
            os.write(master, b"\x02")
            return expect(f"<{name}-{probes[name]}:{expected.hex()}>".encode())

        try:
            expect(b"<ready-codex>")
            original = "中文草稿 ".encode()
            os.write(master, original)
            expect(b"<space-received>")
            os.write(master, b"\\cl")
            expect(b"\\cl")
            os.write(master, b"aude\r")
            expect(b"<ready-claude>")
            (self.path / "go").touch()
            until = time.monotonic() + 5
            while not (self.path / "done").exists() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue((self.path / "done").exists(), "hidden writer stalled")
            os.write(master, b"A \\cod")
            expect(b"\\cod")
            os.write(master, b"\x1b[O\x1b[<64;1;2M\x1bOA\x15cod\x1b\r")
            draft("claude", b"A")
            paste = b" \\codex\r"
            os.write(master, b"\x1b[200~" + paste + b"\x1b[201~")
            output = draft("claude", b"A" + paste)
            self.assertNotIn(b"<background-done>", output)
            os.write(master, b" \\agy\r")
            expect(b"<ready-agy>")
            os.write(master, b"B \\codex\r")
            expect(b"<background-done>")
            draft("codex", original)
            os.write(master, b" \\claude\r")
            draft("claude", b"A" + paste)
            os.write(master, b"\x04")
            expect(b"<background-done>")
            draft("codex", original)
            os.write(master, b"\x04")
            expect(b"<ready-agy>")
            draft("agy", b"B")
            os.write(master, b"\x04")
            expect(b"<ended-agy>")
            # OpenSSH drains the local tty before restoring its attributes.
            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        if not os.read(master, 65536):
                            break
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
            self.assertEqual(proc.wait(timeout=1), 23)
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)

    def test_switching_keeps_live_processes_drafts_and_background_progress(self):
        for source in SOURCES:
            (self.path / source).write_text(SWITCH_CLI)
        self.env["SWITCH_FIXTURE"] = str(self.path)
        self.switch_session(self.command("codex", "-C", str(self.path)))
        for source in SOURCES:
            records = (self.path / (source + ".launches")).read_text().splitlines()
            self.assertEqual(len(records), 1)
            if source != "codex":
                self.assertEqual(records[0].split(" ", 1)[1], os.path.realpath(self.path))
