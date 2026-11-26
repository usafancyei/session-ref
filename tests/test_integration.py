"""Exercise the real terminal selector through the normal zsh entry points."""

import concurrent.futures
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
                    os.write(master, "前后".encode() + b"\x1b[D#")
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
                    os.write(master, b"@\x1b[200~#paste\x1b[201~\x04")
                    continued = True
            expected = "前后".encode() + b"\x1b[D"
            expected += b"#" + after_escape + b"@#paste" if cancel else selection.encode() + b"-ref@"
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
