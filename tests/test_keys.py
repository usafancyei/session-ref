import unittest

from session_ref.keys import DISABLE, ENABLE, END, START, Keys


class KeysTests(unittest.TestCase):
    def test_trigger_and_all_chunk_boundaries_in_paste(self):
        payload = "中文#@@".encode()
        for brackets in (False, True):
            data = b"@x#" + START + payload + END + b"#tail"
            for i in range(len(data) + 1):
                keys = Keys(b"#")
                keys.child_paste = brackets
                events = [*keys.feed(data[:i]), *keys.feed(data[i:])]
                actual = b"".join(b"<pick>" if e is None else e for e in events)
                pasted = START + payload + END if brackets else payload
                self.assertEqual(actual, b"@x<pick>" + pasted + b"<pick>tail")

    def test_split_terminal_modes_and_cursor_sequences(self):
        keys = Keys(b"#")
        self.assertEqual(keys.output(ENABLE), ENABLE)
        self.assertTrue(keys.child_paste)
        self.assertEqual(keys.output(DISABLE[:2]) + keys.output(DISABLE[2:]), DISABLE + ENABLE)
        self.assertFalse(keys.child_paste)
        data = b"\x1b[D\x1b[<35;2;2M\x1b[M###\x1b#"
        self.assertEqual(b"".join(keys.feed(data)), data)

    def test_configured_trigger_and_unicode_keyboard_protocol(self):
        keys = Keys(b"%")
        events = list(keys.feed(b"#%\x1b[37u\x1b[53:37;2u\x1b[37;5u"))
        self.assertEqual(events, [b"#", None, None, None, b"\x1b[37;5u"])

    def test_standalone_escape_can_flush(self):
        keys = Keys(b"#")
        self.assertEqual(list(keys.feed(b"\x1b")), [])
        self.assertEqual(keys.flush_escape(), b"\x1b")
        self.assertEqual(list(keys.feed(b"#")), [None])

    def test_terminal_reply_with_hash_and_split_terminator(self):
        for terminator in (b"\x07", b"\x1b\\"):
            reply = b"\x1b]11;#121212" + terminator
            for i in range(len(reply) + 1):
                keys = Keys(b"#")
                events = [*keys.feed(reply[:i]), *keys.feed(reply[i:] + b"#")]
                self.assertIsNone(events.pop())
                self.assertEqual(b"".join(events), reply)

    def test_compound_paste_modes_at_every_read_boundary(self):
        for toggle in (b"\x1b[?1049;2004h", b"\x1b[?2004;1004l"):
            enabled = toggle.endswith(b"h")
            for i in range(len(toggle) + 1):
                keys = Keys(b"#")
                keys.child_paste = not enabled
                out = keys.output(toggle[:i]) + keys.output(toggle[i:] + b"x")
                self.assertEqual(out, toggle + (b"" if enabled else ENABLE) + b"x")
                data = list(keys.feed(START + b"#\n" + END))
                self.assertEqual(b"".join(data), START + b"#\n" + END if enabled else b"#\n")

    def test_unfinished_reply_resynchronizes_on_idle_or_interrupt(self):
        for prefix in (b"\x1b]", b"\x1bP", b"\x1b_", b"\x1b^", b"\x1bX"):
            keys = Keys(b"#")
            self.assertEqual(b"".join(keys.feed(prefix + b"text")), prefix + b"text")
            self.assertTrue(keys.in_control_string)
            self.assertEqual(keys.flush_escape(), b"")
            self.assertEqual(list(keys.feed(b"#")), [None])
            list(keys.feed(prefix))
            self.assertEqual(list(keys.feed(b"\x03#")), [b"\x03", None])
            list(keys.feed(prefix + b"\x1b"))
            self.assertEqual(keys.flush_escape(), b"\x1b")

    def test_reported_trigger_with_lock_flags_and_release(self):
        keys = Keys(b"#")
        for flags in (66, 130, 194):
            down = f"\x1b[51:35;{flags}:1u".encode()
            release = f"\x1b[51:35;{flags}:3u".encode()
            self.assertEqual(list(keys.feed(down + release)), [None, release])
