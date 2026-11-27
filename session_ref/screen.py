"""Track replayable drawing and input modes for each child terminal."""

import re


MODES = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
RESET_FRAME = b"\x1b(B\x1b[0m\x1b[r\x1b[2J\x1b[H"
LIMIT = 512 * 1024
# Keep complete drawing operations only. OSC/DCS, queries and bells must
# never be replayed when returning to a child.
OUTPUT = re.compile(
    rb"[^\x00-\x1f\x7f]+|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-~]|"
    rb"\x1b[^\[\]()P_^X]|[\x00-\x1a\x1c-\x1f\x7f]"
)
PARTIAL = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|[()])?")
DRAW = re.compile(rb"\x1b\[[0-9;:]*[mHfABCDEFGIJKLMPSTX@\x60abdrsu]|\x1b[78DEM]|\x1b\([B0]")
STRING_START = (b"\x1b]", b"\x1bP", b"\x1b_", b"\x1b^", b"\x1bX")
STRING_END = re.compile(rb"\x07|\x1b\\|[\x18\x1a]")
INPUT_MODES = {1, 7, 25, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 1016, 2026}
DEFAULT_ON = {7, 25}
KEYBOARD = re.compile(rb"\x1b\[([<>=])([0-9]*)(?:;([123]))?u")
DEVICE_QUERY = re.compile(rb"\x1b\[(?:\??6n|[>=]?0?c|\?[0-9]{1,5}\$p|(?:14|16|18)t)")
C1_CONTROLS = dict.fromkeys(range(0x80, 0xa0))


def frame_bytes(frame):
    return frame.decode("utf-8", errors="replace").translate(C1_CONTROLS).encode()


def private_mode(flag, enabled):
    return b"\x1b[?" + str(flag).encode() + (b"h" if enabled else b"l")


class Screen:
    def __init__(self):
        self.alternate = False
        self.cursor_visible = True
        self.primary = bytearray()
        self.history = None
        self.complete = True
        self.pending = b""
        self.string = False
        self.modes = {}
        self.primary_keyboard = [0]
        self.alternate_keyboard = [0]
        self.keypad = False
        self.modify_keys = 0
        self.child_paste = False
        self.queries = bytearray()

    @property
    def keyboard(self):
        return self.alternate_keyboard if self.alternate else self.primary_keyboard

    def remember(self, data):
        frame = self.history if self.alternate else self.primary
        if frame is None:
            return
        frame.extend(data)
        if len(frame) > LIMIT:
            # Keep a recent viewport for reattachment. Request a redraw when
            # an alternate-screen application has lost its complete frame.
            if self.alternate:
                self.complete = False
            start = len(frame) - LIMIT // 2
            newline = frame.find(b"\n", start)
            if newline >= 0:
                start = newline + 1
            else:
                escape = frame.rfind(b"\x1b", 0, start)
                token = DRAW.match(frame, escape) if escape >= 0 else None
                if token and start < token.end():
                    start = token.end()
            del frame[:start]
            while frame and 0x80 <= frame[0] <= 0xbf:
                del frame[:1]

    def mode(self, token):
        match = MODES.fullmatch(token)
        if match:
            enabled = match[2] == b"h"
            for flag in match[1].split(b";"):
                if flag in (b"47", b"1047", b"1049"):
                    if self.alternate != enabled:
                        self.history = bytearray() if enabled else None
                        self.complete = True
                        self.alternate = enabled
                elif flag == b"2004":
                    self.child_paste = enabled
                elif flag and len(flag) < 6 and int(flag) in INPUT_MODES:
                    self.modes[int(flag)] = enabled
                    if flag == b"25":
                        self.cursor_visible = enabled
        elif match := KEYBOARD.fullmatch(token):
            value = int(match[2] or b"0") if len(match[2]) < 6 else 0
            if match[1] == b"<":
                del self.keyboard[max(1, len(self.keyboard) - max(1, value)):]
            else:
                flags = self.keyboard[-1]
                if match[1] == b">":
                    if len(self.keyboard) < 64:
                        self.keyboard.append(flags)
                self.keyboard[-1] = (flags | value if match[3] == b"2" else
                                     flags & ~value if match[3] == b"3" else value)
        elif token in (b"\x1b=", b"\x1b>"):
            self.keypad = token == b"\x1b="
        elif match := re.fullmatch(rb"\x1b\[>4(?:;([012]))?m", token):
            self.modify_keys = int(match[1] or b"0")

    def observe(self, data, reply=None):
        data = self.pending + data
        self.pending = b""
        pos = 0
        while pos < len(data):
            if self.string:
                end = STRING_END.search(data, pos)
                if end is None:
                    self.pending = b"\x1b" if data.endswith(b"\x1b") else b""
                    break
                self.string = False
                pos = end.end()
            elif data.startswith(STRING_START, pos):
                self.string = True
                pos += 2
            elif token := OUTPUT.match(data, pos):
                raw = token[0]
                pos = token.end()
                if raw.startswith(b"\x1b"):
                    if reply is not None:
                        if raw == b"\x1b[5n":
                            reply(b"\x1b[0n")
                        elif raw == b"\x1b[?u":
                            reply(f"\x1b[?{self.keyboard[-1]}u".encode())
                        elif DEVICE_QUERY.fullmatch(raw) and len(self.queries) < 4096:
                            # Cursor position and device attributes depend on
                            # the real display. Ask once its owner is visible.
                            self.queries.extend(raw)
                    self.mode(raw)
                    if not DRAW.fullmatch(raw):
                        continue
                    if raw == b"\x1b[2J" and self.alternate and not self.complete:
                        self.history = bytearray()
                        self.complete = True
                elif (raw[0] < 32 and raw not in (b"\r", b"\n", b"\t", b"\b")) or raw == b"\x7f":
                    continue
                self.remember(raw)
            else:
                # A read may split a CSI or charset selection anywhere.
                if len(data) - pos <= 256 and PARTIAL.fullmatch(data, pos):
                    self.pending = data[pos:]
                    break
                pos += 1

    def replay(self):
        if self.history is not None and self.complete:
            return frame_bytes(self.history)
        return None

    def restore(self):
        frame = RESET_FRAME + frame_bytes(self.primary)
        frame += keyboard_modes(self.primary_keyboard, True)
        if self.alternate or self.alternate_keyboard != [0]:
            frame += b"\x1b[?1049h" + keyboard_modes(self.alternate_keyboard, True)
            if self.alternate:
                frame += RESET_FRAME + frame_bytes(self.history or b"")
            else:
                frame += b"\x1b[?1049l"
        return frame + self.input_modes(True)

    def release(self, overlay=False):
        # Kitty's two stacks belong to the physical main/alternate buffers.
        alternate = keyboard_modes(self.alternate_keyboard, False)
        if self.alternate or overlay:
            leave = alternate + b"\x1b[?1049l"
        elif alternate:
            leave = b"\x1b[?1049h" + alternate + b"\x1b[?1049l"
        else:
            leave = b""
        return leave + keyboard_modes(self.primary_keyboard, False) + self.input_modes(False)

    def input_modes(self, restore):
        result = bytearray()
        for flag, enabled in self.modes.items():
            if enabled != (flag in DEFAULT_ON):
                result.extend(private_mode(flag, enabled if restore else not enabled))
        if self.keypad:
            result.extend(b"\x1b=" if restore else b"\x1b>")
        if self.modify_keys:
            result.extend(f"\x1b[>4;{self.modify_keys if restore else 0}m".encode())
        return bytes(result)


def keyboard_modes(stack, restore):
    if restore:
        base = f"\x1b[={stack[0]}u".encode() if stack[0] else b""
        return base + b"".join(f"\x1b[>{flags}u".encode() for flags in stack[1:])
    pop = f"\x1b[<{len(stack) - 1}u".encode() if len(stack) > 1 else b""
    return pop + (b"\x1b[=0u" if stack[0] else b"")
