"""Split terminal input into literal bytes and picker requests (None)."""

import re

from .screen import MODES

START = b"\x1b[200~"
END = b"\x1b[201~"
ENABLE = b"\x1b[?2004h"
DISABLE = b"\x1b[?2004l"
CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
CSI_U = re.compile(rb"\x1b\[(\d+)(?::(\d*))?(?::\d*)?(?:;(\d+)(?::(\d+))?)?(?:;(\d+))?u")
NAVIGATION = re.compile(rb"\x1b\[(1|5|6);(\d+)(?::([123]))?([AB~])")


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
    key, shifted, mods, event, text = match.groups()
    if event not in (None, b"1", b"2"):
        return None
    flags = modifiers(mods)
    if flags == 4 and key in (b"99", b"117"):
        return int(key) - 96
    if flags not in (0, 1):
        return None
    code = int(text or (shifted if flags == 1 else None) or key)
    return code if code <= 0x10ffff and not 0xd800 <= code <= 0xdfff else None


class Keys:
    def __init__(self, trigger):
        self.trigger_key = trigger[-1:]
        self.requires_space = trigger.startswith(b" ")
        self.after_space = False
        self.pending = b""
        self.pasting = False
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
        if self.after_space and token.startswith(b"\x1b["):
            report = CSI_U.fullmatch(token)
            # Releases and standalone modifiers (e.g. Shift for '#') leave
            # the preceding text keystroke unchanged.
            if report and (report[4] == b"3" or 57441 <= int(report[1]) <= 57454):
                return token
        activate = code == self.trigger_key[0] and (self.after_space or not self.requires_space)
        self.after_space = code == 32
        return None if activate else token

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
                    yield data[pos:len(data) - len(self.pending)]
                    return
                yield data[pos:end]
                if self.keep_markers:
                    yield END
                pos = end + len(END)
                self.pasting = False
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
                        self.pasting = True
                        self.keep_markers = self.child_paste or self.selecting
                        if self.keep_markers:
                            yield token
                    else:
                        yield self.key_event(token, reported_key(token))
                        continue
                elif data[pos:pos + 2] == b"\x1bO":
                    if len(data) == pos + 2:
                        self.pending = data[pos:]
                        return
                    yield data[pos:pos + 3]
                    pos += 3
                elif data[pos:pos + 2] in (b"\x1b]", b"\x1bP", b"\x1b_", b"\x1b^", b"\x1bX"):
                    self.in_control_string = True
                    yield data[pos:pos + 2]
                    pos += 2
                elif (data[pos:] == b"\x1b" or re.fullmatch(
                        rb"\x1b\[[0-?]*[ -/]*", data[pos:])) and len(data) - pos < 128:
                    self.pending = data[pos:]
                    return
                else:
                    yield data[pos:pos + 2]
                    pos += 2
                self.after_space = False
            else:
                escape = data.find(b"\x1b", pos)
                stop = len(data) if escape < 0 else escape
                for i, piece in enumerate(data[pos:stop].split(self.trigger_key)):
                    if i:
                        yield self.key_event(self.trigger_key, self.trigger_key[0])
                    if piece:
                        self.after_space = piece.endswith(b" ")
                        yield piece
                pos = stop

    def flush_escape(self):
        self.after_space = False
        data, self.pending = self.pending, b""
        self.in_control_string = False
        return data
