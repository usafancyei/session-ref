"""Restore full-screen CLIs whose alternate screen cannot be nested."""

import re


MODES = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
RESET_FRAME = b"\x1b(B\x1b[0m\x1b[r\x1b[2J\x1b[H"
# Only replay text and drawing operations. Queries, OSC controls (including
# clipboard operations), bells and device mode changes must never run twice.
TOKENS = re.compile(
    r"(?P<text>[^\x00-\x1f\x7f-\x9f]+|[\r\n\t\b])"
    r"|(?P<draw>\x1b\[[0-9;:]*[mHfABCDEFGIJKLMPSTX@d`abrsu]|\x1b[78DEM]|\x1b\([B0])"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]|\x07"
)


class Screen:
    def __init__(self):
        self.alternate = False
        self.cursor_visible = True
        self.history = None
        self.tail = b""

    def observe(self, data):
        combined = self.tail + data
        offset = len(self.tail)
        for mode in MODES.finditer(combined):
            if mode.end() <= offset:
                continue
            flags = mode[1].split(b";")
            enabled = mode[2] == b"h"
            if b"25" in flags:
                self.cursor_visible = enabled
            if any(flag in (b"47", b"1047", b"1049") for flag in flags):
                if enabled != self.alternate:
                    self.alternate = enabled
                    self.history = bytearray() if enabled and b"1049" in flags else None
                    offset = mode.end()
        if self.history is not None:
            self.history.extend(combined[offset:])
            if len(self.history) > 512 * 1024:
                self.history = None
        self.tail = combined[-64:]

    def replay(self):
        if self.history is None:
            return None
        text = self.history.decode("utf-8", errors="replace")
        parts = []
        pos = 0
        while pos < len(text):
            token = TOKENS.match(text, pos)
            if not token:
                return None
            if token.lastgroup:
                parts.append(token[0])
            pos = token.end()
        return "".join(parts).encode()
