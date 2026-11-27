"""Search and select a session on the terminal that owns the current draft."""

import codecs
import json
import os
import re
import signal
import subprocess
import sys
import threading

from .catalog import SAFE_ID, SOURCES, clean, project_root, recent_sessions
from .keys import END, START, key_transition, navigation_key, reported_key
from .view import LABEL, clip, render_picker


class SwitchCommand:
    eof = True

    def __init__(self, prefix):
        self.prefix = prefix
        self.letters = []
        self.name = ""
        self.error = ""

    @property
    def raw(self):
        return self.prefix + b"".join(self.letters)

    def input(self, token):
        """Return a target name on Enter, or literal bytes when leaving the prompt."""
        if key_transition(token):
            return None
        code = reported_key(token)
        if code is None and len(token) == 1:
            code = token[0]
        if code in (27, 3):
            return b""
        if token.startswith(b"\x1b") and code is None:
            if len(token) == 2 and (token[1] < 32 or token[1] == 127):
                return b""
            if len(token) == 2 and token[1] >= 128:
                # The tokenizer may pair Escape with the first byte of UTF-8.
                return token[1:]
            return None
        if code in (10, 13):
            if self.name in SOURCES:
                return self.name
            self.error = "请输入完整名称，或按 Esc 取消"
            return None
        self.error = ""
        if code == 21:
            self.name = ""
            self.letters.clear()
            return None
        if code in (8, 127):
            if not self.name:
                return b""
            self.name = self.name[:-1]
            self.letters.pop()
            return None
        if code is not None and (code < 32 or 127 <= code <= 159 or 57344 <= code <= 63743):
            return None
        text = chr(code) if code is not None else token.decode("ascii", errors="replace")
        name = self.name + text
        if not any(source.startswith(name) for source in SOURCES):
            return self.raw + token
        self.name = name
        # Keep the original encoding of each editable ASCII letter (plain or CSI-u).
        self.letters.extend([token] if code is not None else [bytes([ch]) for ch in token])
        return None

    def render(self, size):
        rows, cols = size
        lines = ["切换 CLI    codex / claude / agy", "\\" + self.name,
                 self.error or "Enter 切换    Esc 取消"]
        return b"\x1b[H\x1b[J" + "\r\n".join(clip(line, cols) for line in lines[:rows]).encode()

    def close(self):
        pass


class Picker:
    def __init__(self, launcher, directory=None):
        self.project = project_root(directory or os.getcwd())
        self.by_project = False
        self.rows = {}
        self.matches = []
        self.query = ""
        self.index = 0
        self.page_up = self.page_down = 1
        self.navigated = False
        self.pending = set(SOURCES)
        self.errors = {}
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pasting = False
        self.buffer = bytearray()
        self.eof = False
        self.loader = subprocess.Popen(
            [sys.executable, launcher, "_stream"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)

    def fileno(self):
        return self.loader.stdout.fileno()

    def read(self):
        data = os.read(self.fileno(), 65536)
        if not data:
            self.eof = True
            for source in self.pending:
                self.errors[source] = "列表连接已关闭"
            self.pending.clear()
            return True
        self.buffer.extend(data)
        last = self.buffer.rfind(b"\n")
        if last == -1:
            return False
        lines = self.buffer[:last].splitlines()
        del self.buffer[:last + 1]
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("source") not in SOURCES:
                continue
            source = event["source"]
            for row in event.get("rows", []):
                self.rows[source, row["id"]] = row
            if event.get("done"):
                self.pending.discard(source)
                if event.get("error"):
                    self.errors[source] = clean(event["error"])
        self.filter()
        return True

    def filter(self, *, keep_selection=False):
        selected = self.matches[self.index] if (self.navigated or keep_selection) and self.matches else None
        terms = self.query.casefold().split()

        def matches(row):
            text = " ".join([LABEL[row["source"]], row["title"], row["project"], row["id"]]).casefold()
            return all(term in text for term in terms)

        self.matches = recent_sessions((row for row in self.rows.values() if matches(row)),
                                       self.project, by_project=self.by_project)
        self.index = next((i for i, row in enumerate(self.matches)
                           if selected and (row["source"], row["id"]) == (selected["source"], selected["id"])), 0)

    def input(self, token):
        """Return (ID or literal-trigger sentinel, trailing draft bytes) on dismissal."""
        if token == START and not self.pasting:
            self.pasting = True
            return
        if self.pasting:
            if token == END:
                self.pasting = False
            else:
                self.edit(self.query + "".join(c if c.isprintable() else " "
                                               for c in self.decoder.decode(token)))
            return

        arrows = {b"\x1b[A": -1, b"\x1bOA": -1, b"\x1b[B": 1, b"\x1bOB": 1,
                  b"\x1b[5~": -self.page_up, b"\x1b[6~": self.page_down}
        token = navigation_key(token)
        if token in arrows:
            self.navigated = True
            self.index = max(0, min(len(self.matches) - 1, self.index + arrows[token]))
            return
        if token.startswith(b"\x1b["):
            # Some CLIs request CSI-u keyboard reporting, including for Return.
            code = reported_key(token)
            if code is None:
                return
            token = chr(code).encode()
        if token.startswith(b"\x1b") and len(token) > 1:
            if token in (b"\x1b\x7f", b"\x1b\x08"):
                self.decoder.reset()
                self.edit(re.sub(r"\S+$", "", self.query.rstrip()))
                return
            if len(token) == 2 and token[1] < 32:
                # Both control keys were delivered to the open picker.
                return None, b""
            # The tokenizer may pair Esc with only the first byte of a UTF-8
            # character. Return that byte unchanged; the rest follows on the PTY.
            if len(token) == 2 and not token[1:].isalpha() and token[1:] != b"[":
                return None, token[1:]
            return
        query = self.query
        for pos, value in enumerate(token):
            if value in (27, 3):
                return None, token[pos + 1:]
            if value in (10, 13):
                self.edit(query)
                if self.matches:
                    identifier = self.matches[self.index]["id"]
                    if SAFE_ID.fullmatch(identifier):
                        return identifier.encode(), token[pos + 1:]
            elif value in (8, 127):
                self.decoder.reset()
                query = query[:-1]
            elif value == 21:
                self.decoder.reset()
                query = ""
            elif value == 9:
                self.edit(query)
                self.by_project = not self.by_project
                self.filter(keep_selection=True)
            elif value >= 32:
                query += "".join(c for c in self.decoder.decode(bytes([value])) if c.isprintable())
        self.edit(query)

    def edit(self, query):
        if query != self.query:
            self.query = query
            self.navigated = False
            self.filter()

    def render(self, size):
        frame, self.page_up, self.page_down = render_picker(
            self.matches, self.index, self.query, self.pending, self.errors, size, self.project, self.by_project)
        return frame

    def close(self):
        self.loader.stdout.close()
        if self.loader.poll() is None:
            try:
                os.killpg(self.loader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin can report EPERM for an exited process group. Reap
                # the owned child; if still exiting, do not stall the draft.
                try:
                    self.loader.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    threading.Thread(target=self.loader.wait, daemon=True).start()
                return
        self.loader.wait()
