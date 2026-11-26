import contextlib
import errno
import os
import pty
import select
import signal
import termios
import time
import tty

from .keys import DISABLE, ENABLE, Keys
from .screen import RESET_FRAME, Screen


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
    def __init__(self, master, trigger, open_picker):
        self.master = master
        self.open_picker = open_picker
        self.keys = Keys(trigger)
        self.screen = Screen()
        self.picker = None
        self.saved_frame = None
        self.deferred = bytearray()
        self.dirty = False
        self.size = termios.tcgetwinsize(0)

    def resize(self, *_):
        size = termios.tcgetwinsize(0)
        if size != self.size:
            self.saved_frame = self.screen.history = None
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
            self.screen.observe(data)
            write_all(1, self.keys.output(data))

    def input(self, token):
        if self.picker:
            if self.keys.in_control_string:
                return
            result = self.picker.input(self.keys.trigger_key if token is None else token)
            self.dirty = True
            if result is not None:
                selected, tail = result
                self.dismiss()
                write_all(self.master, (selected or self.keys.trigger_key) + tail)
        elif token is not None:
            write_all(self.master, token)
        else:
            try:
                self.picker = self.open_picker()
            except OSError:
                write_all(self.master, self.keys.trigger_key)
                return
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

    def dismiss(self):
        self.picker.close()
        self.picker = None
        self.keys.selecting = False
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
        self.resize()
        if self.deferred:
            self.output(bytes(self.deferred))
            self.deferred.clear()


class Disconnected(Exception):
    pass


def run_pty(argv, trigger, open_picker):
    original = termios.tcgetattr(0)
    size = termios.tcgetwinsize(0)
    pid, master = pty.fork()
    if pid == 0:
        try:
            termios.tcsetattr(0, termios.TCSANOW, original)
            termios.tcsetwinsize(0, size)
            os.execv(argv[0], argv)
        except OSError as exc:
            os.write(2, f"session-ref: {exc}\r\n".encode())
        os._exit(126)

    terminal = Terminal(master, trigger, open_picker)
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
            os.killpg(pid, sig)

    handlers = {signal.SIGWINCH: resized, signal.SIGTERM: disconnect,
                signal.SIGHUP: disconnect, signal.SIGINT: interrupt}
    saved = {sig: signal.signal(sig, handler) for sig, handler in handlers.items()}
    keys = terminal.keys
    deadline = 0
    lost = 0
    status = None
    try:
        tty.setraw(0, termios.TCSANOW)
        terminal.resize()
        write_all(1, ENABLE)
        while True:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                if terminal.picker:
                    terminal.dismiss()
                # A descendant can retain the slave after the actual CLI exits.
                while select.select([master], [], [], 0)[0]:
                    try:
                        last = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not last:
                        break
                    terminal.output(last)
                break
            terminal.draw()
            picker = terminal.picker
            watching = [0, resize_read]
            # Bound background output while the selector owns the display.
            if len(terminal.deferred) < 256 * 1024:
                watching.append(master)
            if picker and not picker.eof:
                watching.append(picker)
            timeout = 0.25
            if (keys.pending or keys.in_control_string) and not keys.pasting:
                timeout = max(0, deadline - time.monotonic())
                if timeout == 0:
                    terminal.input(keys.flush_escape())
                    continue
            ready, _, _ = select.select(watching, [], [], timeout)
            if resize_read in ready:
                os.read(resize_read, 4096)
                terminal.resize()
            if master in ready:
                try:
                    output = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not output:
                    break
                terminal.output(output)
            if picker and picker in ready:
                terminal.dirty |= picker.read()
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    raise Disconnected(signal.SIGHUP)
                for token in keys.feed(data):
                    terminal.input(token)
                deadline = time.monotonic() + (0.25 if keys.in_control_string and not keys.pending else 0.08)
    except Disconnected as exc:
        lost = exc.args[0]
    except (BrokenPipeError, OSError) as exc:
        if exc.errno not in (errno.EIO, errno.EPIPE, errno.ENXIO):
            raise
        lost = signal.SIGHUP
    finally:
        if terminal.picker:
            with contextlib.suppress(OSError, termios.error):
                terminal.dismiss()
        with contextlib.suppress(OSError):
            write_all(1, DISABLE)
        with contextlib.suppress(termios.error):
            termios.tcsetattr(0, termios.TCSANOW, original)
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        os.close(resize_write)
        os.close(resize_read)
        os.close(master)
    if status is None:
        status = end_child(pid, lost) if lost else os.waitpid(pid, 0)[1]
        if status is None:
            return 128 + lost
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code
