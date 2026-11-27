import unittest

from session_ref.keys import DISABLE, ENABLE, END, START, CycleDirection, CycleKey, Keys, SwitchPrefix


class KeysTests(unittest.TestCase):
    def test_cycle_shortcuts_at_every_read_boundary(self):
        shortcuts = (
            (CycleDirection.PREVIOUS, b"\x1c", b""),
            (CycleDirection.PREVIOUS, b"\x1b[27;5;92~", b""),
            (CycleDirection.PREVIOUS, b"\x1b[92;5u", b""),
            (CycleDirection.PREVIOUS, b"\x1b[35::92;5u", b"\x1b[35::92;5:2u\x1b[35::92;5:3u"),
            (CycleDirection.PREVIOUS, b"\x1b[92:124;197:1u", b"\x1b[92;197:2u\x1b[92;197:3u"),
            (CycleDirection.NEXT, b"\x1f", b""),
            (CycleDirection.NEXT, b"\x1b[27;5;47~", b""),
            (CycleDirection.NEXT, b"\x1b[47;5u", b""),
            (CycleDirection.NEXT, b"\x1b[35::47;5u", b"\x1b[35::47;5:2u\x1b[35::47;5:3u"),
            (CycleDirection.NEXT, b"\x1b[47:63;197:1u", b"\x1b[47;197:2u\x1b[47;197:3u"),
        )
        for trigger in (b" #", b" \\"):
            for direction, press, tail in shortcuts:
                data = b"a " + press + tail + b"#"
                for cut in range(len(data) + 1):
                    keys = Keys(trigger, switching=True)
                    events = [*keys.feed(data[:cut]), *keys.feed(data[cut:])]
                    self.assertEqual(events, [b"a", b" ", CycleKey(direction, press), b"#"], (press, cut))

    def test_cycle_shortcuts_are_literal_in_payloads_and_outside_workspace_input(self):
        shortcut = b"\x1c\x1b[92;5u\x1f\x1b[47;5u"
        keys = Keys(b" #", switching=True)
        self.assertEqual(b"".join(keys.feed(START + shortcut + END)), shortcut)
        reply = b"\x1b]label\x1c\x1f\x07"
        self.assertEqual(b"".join(keys.feed(reply)), reply)
        others = (b"\\\x1b[92u\x1b[92;3u\x1b[92;6u\x1b[35:92;5u\x1b[35::92;6u"
                  b"/\x1b[47u\x1b[47;3u\x1b[47;6u\x1b[35:47;5u\x1b[35::47;6u\x1b[95;5u")
        self.assertEqual(b"".join(keys.feed(others)), others)
        keys.selecting = True
        self.assertEqual(b"".join(keys.feed(shortcut)), shortcut)
        self.assertEqual(b"".join(Keys(b" #").feed(shortcut)), shortcut)

    def test_only_deliberate_input_interrupts_a_cycle(self):
        reports = (b"\x1b[I\x1b[O\x1b[<64;1;2M\x1b[Mabc\x1b[24;80R\x1b[0n\x1b[?1u"
                   b"\x1b[?1;2c\x1b[?2004;1$y\x1b]11;color\x1b\\\x1bPanswer\x1b\\"
                   b"\x1b[97;1:3u\x1b[57442;5u\x1b[1;5:3A")
        for cut in range(len(reports) + 1):
            keys = Keys(b"#", switching=True)
            for chunk in (reports[:cut], reports[cut:]):
                for token in keys.feed(chunk):
                    self.assertFalse(keys.is_user_input(token), token)
        for data in (b"x", b"\x02", b"\x1b[A", b"\x1bOA", b"\x1b[3~", b"\x1b[1;5:1A",
                     b"\x1b[97;1;97:769u", START + b"\x1b[?1u" + END, b"#", b" \\",
                     b"\x1b[32u\x1b[32;1:3u\x1b]reply\x07"):
            keys = Keys(b"#", switching=True)
            self.assertTrue(any(keys.is_user_input(token) for token in keys.feed(data)), data)

    def test_switch_prefix_and_paste_at_every_read_boundary(self):
        for space in (b" ", b"\x1b[32u\x1b[32;1:3u"):
            for slash in (b"\\", b"\x1b[92u"):
                prefix = space + slash
                paste = START + b" \\agy\r #x" + END
                data = b"a" + prefix + b"claude" + paste + b" #"
                for cut in range(len(data) + 1):
                    keys = Keys(b" #", switching=True)
                    keys.child_paste = True
                    events = [*keys.feed(data[:cut]), *keys.feed(data[cut:])]
                    self.assertEqual(events.count(SwitchPrefix(prefix)), 1)
                    raw = b"".join(b"<switch>" if isinstance(e, SwitchPrefix) else
                                   b"<pick>" if e is None else e for e in events)
                    self.assertEqual(raw, b"a<switch>claude" + paste + b" <pick>")

    def test_explicit_backslash_picker_keeps_its_shortcut(self):
        keys = Keys(b" \\", switching=True)
        self.assertEqual(list(keys.feed(b" \\")), [b" ", None])
        keys = Keys(b"\\", switching=True)
        self.assertEqual(list(keys.feed(b"\\ \\")), [None, SwitchPrefix(b" \\")])

    def test_space_then_hash_across_reads(self):
        space = b"\x1b[32u"
        release = b"\x1b[32;1:3u"
        shift = b"\x1b[57441;2u"
        hash_key = b"\x1b[51:35;2u"
        for prefix in (b" ", space, space + release + shift):
            for key in (b"#", hash_key):
                data = key + b"word#" + prefix + key + key
                for cut in range(len(data) + 1):
                    keys = Keys(b" #")
                    events = [*keys.feed(data[:cut]), *keys.feed(data[cut:])]
                    actual = b"".join(b"<pick>" if e is None else e for e in events)
                    self.assertEqual(actual, key + b"word#" + prefix + b"<pick>" + key)

    def test_space_is_immediate_and_edits_and_paste_break_the_sequence(self):
        for interrupt in (b"\x7f", b"\r", b"\t", b"x", b"\x1b[D", b"\x1bOA",
                          START + b" #" + END, START + b" " + END, START + END):
            keys = Keys(b" #")
            keys.child_paste = True
            self.assertEqual(list(keys.feed(b" ")), [b" "])
            self.assertEqual(b"".join(keys.feed(interrupt + b"#")), interrupt + b"#")
        keys = Keys(b" #")
        list(keys.feed(b" \x1b"))
        self.assertEqual(keys.flush_escape(), b"\x1b")
        self.assertEqual(list(keys.feed(b"#")), [b"#"])

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
