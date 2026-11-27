import contextlib
import errno
import os
import pty
import select
import shutil
import signal
import termios
import time
import tty
from dataclasses import dataclass, field

from .keys import (DISABLE, ENABLE, START, CycleDirection, CycleKey, Keys, SwitchPrefix,
                   key_transition, reported_key)
from .picker import SwitchCommand
from .screen import RESET_FRAME, STRING_END, Screen


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def end_child(pid, sig):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        child, status = os.waitpid(pid, os.WNOHANG)
        if child:
            return status
        time.sleep(0.02)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        child, status = os.waitpid(pid, os.WNOHANG)
        return status if child else None
    return os.waitpid(pid, 0)[1]


class Terminal:
    def __init__(self, master, trigger, open_picker, switch=None, send=None):
        self.master = master
        self.open_picker = open_picker
        self.switch = switch
        self.send = send or (lambda data: write_all(self.master, data))
        self.keys = Keys(trigger, switching=switch is not None)
        self.screen = Screen()
        self.skip_string = False
        self.picker = None
        self.saved_frame = None
        self.deferred = bytearray()
        self.dirty = False
        self.cancel_until = 0.0
        self.size = termios.tcgetwinsize(0)

    def resize(self, *_):
        size = termios.tcgetwinsize(0)
        if size != self.size:
            self.saved_frame = None
            self.screen.complete = False
        self.size = size
        rows, cols = size
        if self.picker and self.screen.alternate and self.saved_frame is None:
            # A long-running TUI may no longer have a replayable frame. Give
            # it a different viewport until dismissal so it repaints at full size.
            rows = max(1, rows - 1)
        termios.tcsetwinsize(self.master, (rows, cols))
        self.dirty = True

    def output(self, data):
        if self.picker:
            self.deferred.extend(data)
        else:
            # An OSC/DCS begun off-screen has no opener on the physical
            # terminal. Consume its remainder in the child's parser only.
            visible = data
            if self.skip_string:
                prefix = self.screen.pending
                end = STRING_END.search(prefix + data)
                visible = data[end.end() - len(prefix):] if end else b""
                self.skip_string = end is None
            self.screen.observe(data)
            write_all(1, self.keys.output(visible))

    def input(self, token):
        if self.cancel_until:
            # Encoded Escape and Enter may arrive as two CSI-u events across reads.
            fast = not self.keys.pasting and time.monotonic() < self.cancel_until
            if fast and isinstance(token, bytes) and key_transition(token):
                return
            if fast and isinstance(token, bytes) and (token in (b"\r", b"\n") or reported_key(token) in (10, 13)):
                return
            self.cancel_until = 0.0
        if isinstance(token, CycleKey):
            switched = self.switch(direction=token.direction)
            if not switched and token.raw == b"\x1f":
                # Legacy terminals also send this byte for Ctrl+_ (undo).
                self.send(token.raw)
        elif isinstance(token, SwitchPrefix):
            self.show(SwitchCommand(token.raw))
        elif isinstance(self.picker, SwitchCommand):
            command = self.picker
            if self.keys.in_control_string:
                return
            if self.keys.pasting and token == START:
                self.keys.keep_markers = self.keys.child_paste
                result = command.raw + (START if self.keys.keep_markers else b"")
            else:
                result = command.input(self.keys.trigger_key if token is None else token)
            self.dirty = True
            if isinstance(result, str):
                self.switch(result)
            elif result is not None:
                if result == b"" and token and (token in (b"\x1b", b"\x1b\r", b"\x1b\n") or reported_key(token) == 27):
                    self.cancel_until = time.monotonic() + 0.08
                self.dismiss()
                self.send(result)
        elif self.picker:
            if self.keys.in_control_string:
                return
            result = self.picker.input(self.keys.trigger_key if token is None else token)
            self.dirty = True
            if result is not None:
                selected, tail = result
                self.dismiss()
                self.send((selected or self.keys.trigger_key) + tail)
        elif token is not None:
            self.send(token)
        else:
            try:
                picker = self.open_picker()
            except OSError:
                self.send(self.keys.trigger_key)
                return
            self.show(picker)

    def show(self, picker):
        self.picker = picker
        self.keys.selecting = True
        if self.screen.alternate:
            self.saved_frame = self.screen.replay()
        else:
            write_all(1, b"\x1b[?1049h")
        write_all(1, RESET_FRAME if self.saved_frame is not None else b"\x1b[0m\x1b[2J\x1b[H")
        write_all(1, b"\x1b[?25l")
        self.resize()

    def draw(self):
        if self.picker and self.dirty:
            self.dirty = False
            rows, cols = self.size
            write_all(1, self.picker.render((rows or 24, cols or 80)))

    def dismiss(self, *, resize_child=True):
        self.picker.close()
        self.picker = None
        self.keys.selecting = False
        self.keys.after_space = False
        if self.saved_frame is not None:
            write_all(1, RESET_FRAME + self.saved_frame)
        elif self.screen.alternate:
            # These bytes belong to the temporarily reduced viewport. Preserve
            # mode switches and queries, then erase that frame before requesting
            # a full-size repaint. A child that left the alternate screen has
            # already restored the primary buffer itself.
            data = bytes(self.deferred)
            self.deferred.clear()
            self.screen.observe(data)
            output = self.keys.output(data)
            if self.screen.alternate:
                output += b"\x1b[2J\x1b[H"
            write_all(1, output)
        else:
            write_all(1, b"\x1b[?1049l")
        write_all(1, b"\x1b[?25h" if self.screen.cursor_visible else b"\x1b[?25l")
        self.saved_frame = None
        if resize_child:
            self.resize()
        if self.deferred:
            self.output(bytes(self.deferred))
            self.deferred.clear()

    def attach(self, child, previous):
        old = self.screen
        write_all(1, b"\x18" + old.release(overlay=self.picker is not None) + b"\x1b[?25h")
        if self.picker:
            self.picker.close()
            self.picker = self.saved_frame = None
            self.keys.selecting = False
            # The old viewport is hidden. Incorporate its last output without
            # printing it or issuing its terminal queries to the new client.
            old.observe(bytes(self.deferred), previous.input_queue.extend)
            self.deferred.clear()
        self.master = child.master
        self.screen = screen = child.screen
        self.keys.space = b""
        self.keys.after_space = self.keys.in_control_string = False
        # If a client exits mid-paste, consume through its (possibly split)
        # end marker before accepting commands for the next foreground client.
        self.keys.discard_paste = self.keys.pasting
        if not self.keys.pasting:
            self.keys.pending = b""
        self.keys.child_paste = screen.child_paste
        self.skip_string = screen.string
        partial = b"" if screen.string else screen.pending
        self.keys.output_tail = partial
        write_all(1, screen.restore() + ENABLE + screen.queries + partial)
        screen.queries.clear()
        self.resize()


class Disconnected(Exception):
    pass


@dataclass
class Child:
    pid: int
    master: int
    screen: Screen
    eof: bool = False
    input_queue: bytearray = field(default_factory=bytearray)

    def flush(self):
        try:
            count = os.write(self.master, self.input_queue)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.ENXIO):
                raise
            self.input_queue.clear()
        else:
            del self.input_queue[:count]


def spawn(argv, original, size, directory=None):
    pid, master = pty.fork()
    if pid == 0:
        try:
            termios.tcsetattr(0, termios.TCSANOW, original)
            termios.tcsetwinsize(0, size)
            if directory is not None:
                os.chdir(directory)
            os.execv(argv[0], argv)
        except OSError as exc:
            os.write(2, f"session-ref: {exc}\r\n".encode())
        os._exit(126)
    os.set_blocking(master, False)
    return Child(pid, master, Screen())


class Workspace:
    def __init__(self, source, argv, directory, original, trigger, open_picker):
        self.original = original
        self.directory = directory
        self.active = spawn(argv, original, termios.tcgetwinsize(0))
        # Insertion order is the activation order, used when a CLI exits.
        self.children = {source: self.active}
        self.cycle_order = ()
        self.terminal = Terminal(self.active.master, trigger, open_picker,
                                 self.switch if source else None,
                                 lambda data: self.active.input_queue.extend(data))
        self.terminal.screen = self.active.screen

    def switch(self, source=None, *, direction=CycleDirection.PREVIOUS):
        if source is None:
            if len(self.children) < 2:
                return False
            # Freeze the order across presses while tracking real activations for exit fallback.
            if not self.cycle_order:
                self.cycle_order = tuple(self.children)
            order = [name for name in self.cycle_order if not self.children[name].eof]
            if len(order) < 2:
                return False
            current = next(reversed(self.children))
            source = order[(order.index(current) + direction.value) % len(order)]
        else:
            self.cycle_order = ()
        child = self.children.get(source)
        if child is self.active:
            self.terminal.dismiss()
            return
        if child is None:
            path = shutil.which(source)
            if path is None:
                self.terminal.picker.error = f"PATH 中找不到 {source}"
                return
            try:
                child = spawn([os.path.abspath(path)], self.original, self.terminal.size, self.directory)
            except OSError as exc:
                self.terminal.picker.error = f"无法启动 {source}（{exc.strerror}）"
                return
            self.children[source] = child
        self.park(self.active)
        self.children.pop(source, None)
        self.children[source] = child
        self.activate(child)
        return True

    def activate(self, child):
        previous = self.active
        self.active = child
        self.terminal.attach(child, previous)

    def park(self, child):
        # Restore the actual height on activation to request a full TUI repaint,
        # including when its bounded drawing history no longer has a full frame.
        rows, cols = self.terminal.size
        termios.tcsetwinsize(child.master, (max(1, rows - 1), cols))

    def read(self, child):
        try:
            data = os.read(child.master, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:
            child.eof = True
        elif child is self.active:
            self.terminal.output(data)
        else:
            child.screen.observe(data, child.input_queue.extend)

    def reap(self):
        for source, child in list(self.children.items()):
            pid, status = os.waitpid(child.pid, os.WNOHANG)
            if not pid:
                continue
            if child is self.active and self.terminal.picker:
                self.terminal.dismiss(resize_child=False)
            # A descendant can retain the slave after the actual CLI exits.
            for _ in range(16):
                if child.eof or not select.select([child.master], [], [], 0)[0]:
                    break
                self.read(child)
            del self.children[source]
            self.cycle_order = ()
            os.close(child.master)
            if not self.children:
                return status
            if child is self.active:
                self.activate(next(reversed(self.children.values())))
        return None

    def resize(self):
        previous = self.terminal.size
        self.terminal.resize()
        for child in self.children.values():
            if child is not self.active:
                if previous != self.terminal.size:
                    child.screen.complete = False
                self.park(child)

    def input(self, token):
        if self.cycle_order and self.terminal.keys.is_user_input(token):
            self.cycle_order = ()
        self.terminal.input(token)

    def run(self, resize_read):
        terminal = self.terminal
        keys = terminal.keys
        deadline = 0
        while True:
            status = self.reap()
            if status is not None:
                return status
            terminal.draw()
            picker = terminal.picker
            watching = [resize_read]
            if len(self.active.input_queue) < 65536:
                watching.append(0)
            writable = []
            for child in self.children.values():
                if child.input_queue and not child.eof:
                    writable.append(child.master)
                if not child.eof and (child is not self.active or len(terminal.deferred) < 256 * 1024):
                    watching.append(child.master)
            if picker and not picker.eof:
                watching.append(picker)
            timeout = 0.25
            if keys.space:
                remaining = keys.space_deadline - time.monotonic()
                if remaining <= 0:
                    for token in keys.literal_space():
                        self.input(token)
                    continue
                timeout = min(timeout, remaining)
            if (keys.pending or keys.in_control_string) and not keys.pasting:
                timeout = min(timeout, max(0, deadline - time.monotonic()))
                if timeout == 0:
                    # A timed-out OSC/DCS fragment still belongs to the terminal.
                    dispatch = terminal.input if keys.in_control_string else self.input
                    dispatch(keys.flush_escape())
                    continue
            ready, writers, _ = select.select(watching, writable, [], timeout)
            if resize_read in ready:
                os.read(resize_read, 4096)
                self.resize()
            for child in self.children.values():
                if child.master in writers:
                    child.flush()
                if child.master in ready:
                    self.read(child)
            if picker and picker in ready:
                terminal.dirty |= picker.read()
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    raise Disconnected(signal.SIGHUP)
                for token in keys.feed(data):
                    if not self.active.eof:
                        self.input(token)
                deadline = time.monotonic() + (0.25 if keys.in_control_string and not keys.pending else 0.08)

    def close(self, sig):
        for child in self.children.values():
            os.close(child.master)
            end_child(child.pid, sig)


def run_pty(argv, trigger, open_picker, *, source=None, directory=None):
    original = termios.tcgetattr(0)
    workspace = Workspace(source, argv, directory, original, trigger, open_picker)
    terminal = workspace.terminal
    resize_read, resize_write = os.pipe()
    os.set_blocking(resize_write, False)

    def resized(_sig, _frame):
        # Wake select even when Python restarts it after SIGWINCH.
        with contextlib.suppress(BlockingIOError):
            os.write(resize_write, b"r")

    def disconnect(sig, _frame):
        raise Disconnected(sig)

    def interrupt(sig, _frame):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(workspace.active.pid, sig)

    handlers = {signal.SIGWINCH: resized, signal.SIGTERM: disconnect,
                signal.SIGHUP: disconnect, signal.SIGINT: interrupt}
    saved = {sig: signal.signal(sig, handler) for sig, handler in handlers.items()}
    lost = signal.SIGHUP
    try:
        tty.setraw(0, termios.TCSANOW)
        terminal.resize()
        write_all(1, ENABLE)
        status = workspace.run(resize_read)
        code = os.waitstatus_to_exitcode(status)
        return code if code >= 0 else 128 - code
    except Disconnected as exc:
        lost = exc.args[0]
    except (BrokenPipeError, OSError) as exc:
        if exc.errno not in (errno.EIO, errno.EPIPE, errno.ENXIO):
            raise
    finally:
        if terminal.picker:
            with contextlib.suppress(OSError, termios.error):
                terminal.dismiss()
        with contextlib.suppress(OSError):
            write_all(1, b"\x18" + terminal.screen.release() + DISABLE)
        with contextlib.suppress(termios.error):
            termios.tcsetattr(0, termios.TCSANOW, original)
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        os.close(resize_write)
        os.close(resize_read)
        workspace.close(lost)
    return 128 + lost
