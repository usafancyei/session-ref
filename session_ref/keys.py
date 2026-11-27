"""Decode terminal input into literal bytes, picker requests and switch prefixes."""

import re
import time
from dataclasses import dataclass
from enum import Enum

from .screen import MODES

START = b"\x1b[200~"
END = b"\x1b[201~"
ENABLE = b"\x1b[?2004h"
DISABLE = b"\x1b[?2004l"
CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
SS3 = re.compile(rb"\x1bO[0-?]*[ -/]*[@-~]")
SPACE_GRACE = 0.15
CSI_U = re.compile(rb"\x1b\[(?P<key>\d+)(?::(?P<shifted>\d*))?(?::(?P<base>\d*))?"
                   rb"(?:;(?P<mods>\d+)(?::(?P<event>\d+))?)?(?:;(?P<text>\d+(?::\d+)*))?u")
CSI_KEY = re.compile(rb"\x1b\[[\d;]*(?::([123]))?[A-FHPQSZ~]")
NAVIGATION = re.compile(rb"\x1b\[(1|5|6);(\d+)(?::([123]))?([AB~])")


class CycleDirection(Enum):
    PREVIOUS = -1
    NEXT = 1


CYCLE_KEYCODES = {b"92": CycleDirection.PREVIOUS, b"47": CycleDirection.NEXT}


def modifiers(value):
    # CSI encodes one plus a bitmask. Caps Lock and Num Lock are state,
    # not shortcut modifiers.
    return (int(value or b"1") - 1) & ~(64 | 128)


def navigation_key(token):
    match = NAVIGATION.fullmatch(token)
    if match and modifiers(match[2]) == 0 and match[3] != b"3":
        if match[1] == b"1" and match[4] in (b"A", b"B"):
            return b"\x1b[" + match[4]
        if match[1] in (b"5", b"6") and match[4] == b"~":
            return b"\x1b[" + match[1] + b"~"
    return token


def reported_key(seq):
    """Decode text and the picker's control shortcuts, ignoring releases."""
    match = CSI_U.fullmatch(seq)
    if not match:
        return None
    key, shifted, _, mods, event, text = match.groups()
    if event not in (None, b"1", b"2"):
        return None
    flags = modifiers(mods)
    if flags == 4 and key in (b"99", b"117"):
        return int(key) - 96
    if flags not in (0, 1) or (text and b":" in text):
        return None
    code = int(text or (shifted if flags == 1 else None) or key)
    return code if code <= 0x10ffff and not 0xd800 <= code <= 0xdfff else None


def key_transition(token):
    report = CSI_U.fullmatch(token)
    return report and (report["event"] == b"3" or 57441 <= int(report["key"]) <= 57454)


def cycle_shortcut(token):
    """Return the CLI cycle direction and keyboard event for a shortcut."""
    if token in (b"\x1c", b"\x1b[27;5;92~"):  # legacy byte or xterm modifyOtherKeys
        return CycleDirection.PREVIOUS, b"1"
    if token in (b"\x1f", b"\x1b[27;5;47~"):
        return CycleDirection.NEXT, b"1"
    report = CSI_U.fullmatch(token)
    if report and modifiers(report["mods"]) == 4:
        direction = CYCLE_KEYCODES.get(report["key"]) or CYCLE_KEYCODES.get(report["base"])
        if direction is not None:
            return direction, report["event"] or b"1"


@dataclass(frozen=True)
class CycleKey:
    direction: CycleDirection
    raw: bytes


@dataclass(frozen=True)
class SwitchPrefix:
    raw: bytes


class Keys:
    def __init__(self, trigger, switching=False):
        self.trigger_key = trigger[-1:]
        self.requires_space = trigger.startswith(b" ")
        self.switching = switching
        self.switch_prefix = switching and trigger != b" \\"
        self.space = b""
        self.space_deadline = 0.0
        self.special = re.compile(rb"[\x00-\x20\x7f" + re.escape(self.trigger_key + b"\\") + b"]")
        self.after_space = False
        self.pending = b""
        self.pasting = False
        self.discard_paste = False
        self.keep_markers = False
        self.child_paste = False
        self.selecting = False
        self.output_tail = b""
        self.in_control_string = False

    def output(self, data):
        # Keep the outer terminal tagging pastes even when the child disables
        # tagging. Deliver markers to the child only while it requests them.
        combined = self.output_tail + data
        offset = len(self.output_tail)
        chunks = []
        for match in MODES.finditer(combined):
            if match.end() <= offset or b"2004" not in match[1].split(b";"):
                continue
            chunks.append(combined[offset:match.end()])
            self.child_paste = match[2] == b"h"
            if not self.child_paste:
                chunks.append(ENABLE)
            offset = match.end()
        chunks.append(combined[offset:])
        self.output_tail = combined[-64:]
        return b"".join(chunks)

    def key_event(self, token, code):
        if self.after_space and key_transition(token):
            return token
        activate = code == self.trigger_key[0] and (self.after_space or not self.requires_space)
        self.after_space = code == 32
        return None if activate else token

    def stroke(self, token, code):
        if self.switching and not self.selecting:
            shortcut = cycle_shortcut(token)
            if shortcut is not None:
                direction, event = shortcut
                if event == b"1":
                    yield from self.literal_space()
                    self.after_space = False
                    yield CycleKey(direction, token)
                return
        if self.switch_prefix and not self.selecting:
            if self.after_space and code == ord("\\"):
                prefix, self.space = self.space + token, b""
                self.after_space = False
                yield SwitchPrefix(prefix)
                return
            if self.space:
                if key_transition(token) and len(self.space) < 1024:
                    self.space += token
                    return
                space, self.space = self.space, b""
                yield space
            if code == 32:
                self.space = token
                self.space_deadline = time.monotonic() + SPACE_GRACE
                self.after_space = True
                return
        yield self.key_event(token, code)

    def literal_space(self):
        if self.space:
            space, self.space = self.space, b""
            yield space

    def is_user_input(self, token):
        """True for an edit, keypress or picker request that ends a shortcut cycle."""
        if isinstance(token, CycleKey) or self.in_control_string:
            return False
        if not isinstance(token, bytes):  # picker request or switch prefix
            return True
        if self.pasting or not token.startswith(b"\x1b["):
            return bool(token)
        # A buffered space can include subsequent modifier/release reports.
        report = CSI_U.match(token)
        if report:
            return not key_transition(report[0])
        # Recognize CSI keypresses explicitly. Other CSI includes focus, mouse
        # and terminal replies such as cursor position and keyboard mode.
        key = CSI_KEY.fullmatch(token)
        return key is not None and key[1] != b"3"

    def feed(self, data):
        data = self.pending + data
        self.pending = b""
        pos = 0
        while pos < len(data):
            if self.in_control_string:
                # OSC/DCS replies may contain printable text, including '#'.
                term = re.search(rb"[\x03\x07\x18\x1a\x1b]", data[pos:])
                if not term:
                    yield data[pos:]
                    return
                stop = pos + term.start()
                if stop > pos:
                    yield data[pos:stop]
                pos = stop
                if data[pos:] == b"\x1b":
                    self.pending = b"\x1b"
                    return
                if data.startswith(b"\x1b\\", pos):
                    yield data[pos:pos + 2]
                    pos += 2
                elif data[pos] == 7:
                    yield data[pos:pos + 1]
                    pos += 1
                # A new escape sequence or cancellation interrupts the string.
                self.in_control_string = False
            elif self.pasting:
                end = data.find(END, pos)
                if end == -1:
                    # Withhold only the possible partial end marker.
                    for size in range(min(len(data) - pos, len(END) - 1), 0, -1):
                        if data.endswith(END[:size]):
                            self.pending = END[:size]
                            break
                    if not self.discard_paste:
                        yield data[pos:len(data) - len(self.pending)]
                    return
                if not self.discard_paste:
                    yield data[pos:end]
                    if self.keep_markers:
                        yield END
                pos = end + len(END)
                self.pasting = False
                self.discard_paste = False
            elif data[pos] == 27:
                seq = CSI.match(data, pos)
                if seq:
                    token = seq[0]
                    if token == b"\x1b[M":  # X10 mouse report + three position bytes
                        if len(data) < seq.end() + 3:
                            self.pending = data[pos:]
                            return
                        token = data[pos:seq.end() + 3]
                    pos += len(token)
                    if token == START:
                        yield from self.literal_space()
                        self.pasting = True
                        self.keep_markers = self.child_paste or self.selecting
                        if self.keep_markers:
                            yield token
                    else:
                        yield from self.stroke(token, reported_key(token))
                        continue
                elif data[pos:pos + 2] == b"\x1bO":
                    seq = SS3.match(data, pos)
                    if not seq and len(data) - pos < 128 and re.fullmatch(rb"\x1bO[0-?]*[ -/]*", data[pos:]):
                        self.pending = data[pos:]
                        return
                    yield from self.literal_space()
                    end = seq.end() if seq else pos + 2
                    yield data[pos:end]
                    pos = end
                elif data[pos:pos + 2] in (b"\x1b]", b"\x1bP", b"\x1b_", b"\x1b^", b"\x1bX"):
                    yield from self.literal_space()
                    self.in_control_string = True
                    yield data[pos:pos + 2]
                    pos += 2
                elif (data[pos:] == b"\x1b" or re.fullmatch(
                        rb"\x1b\[[0-?]*[ -/]*", data[pos:])) and len(data) - pos < 128:
                    self.pending = data[pos:]
                    return
                else:
                    yield from self.literal_space()
                    end = pos + (1 if data.startswith(b"\x1b\x1b", pos) else 2)
                    yield data[pos:end]
                    pos = end
                self.after_space = False
            else:
                escape = data.find(b"\x1b", pos)
                stop = len(data) if escape < 0 else escape
                for match in self.special.finditer(data, pos, stop):
                    if match.start() > pos:
                        yield from self.stroke(data[pos:match.start()], None)
                    token = match[0]
                    yield from self.stroke(token, token[0])
                    pos = match.end()
                if pos < stop:
                    yield from self.stroke(data[pos:stop], None)
                pos = stop

    def flush_escape(self):
        self.after_space = False
        data, self.space, self.pending = self.space + self.pending, b"", b""
        self.in_control_string = False
        return data
