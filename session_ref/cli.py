import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .catalog import SOURCES, clean, collect, display_title, project_root, recent_sessions
from .picker import Picker
from .terminal import run_pty


ROOT = Path(__file__).resolve().parent.parent
COMMANDS = {
    "codex": set("agents exec e review login logout mcp plugin app-server remote-control app "
                 "completion update doctor sandbox debug apply a queue archive delete migrate-rollouts "
                 "migrate-nfs-db unarchive cloud exec-server features help".split()),
    "claude": set("agents auth auto-mode doctor gateway import install logs mcp plugin plugins purge "
                  "respawn rm setup-token stop kill ultrareview update upgrade help".split()),
    "agy": set("acp agent agents changelog help install mcp mic-serve models plugin plugins "
               "remote-control update".split()),
}
VALUES = {
    "codex": set("-c --config --enable --disable --remote --remote-auth-token-env -i --image "
                 "-m --model -p --profile -s --sandbox -C --cd --add-dir -a --ask-for-approval".split()),
    "claude": set("--model --agent --effort --settings --session-id --system-prompt "
                  "--permission-mode --add-dir --append-system-prompt --plugin-dir -n --name".split()),
    "agy": set("--add-dir --agent --effort --mode --model --project --settings "
               "-r --resume --conversation -i --prompt-interactive".split()),
}


def interactive(source, args):
    bypass = {"-h", "--help", "--version"}
    if source == "codex":
        bypass.add("-V")
    else:
        bypass.update({"-v", "-p", "--print", "--output-format", "--input-format"})
    if source == "claude":
        bypass.update({"--bg", "--background", "--desktop", "--cloud"})
    if source == "agy":
        bypass.add("--prompt")
    value_expected = None
    positional = False
    for arg in args:
        if value_expected:
            consume = value_expected == "required" or not arg.startswith("-")
            value_expected = None
            if consume:
                continue
        if arg == "--":
            break
        flag = arg.split("=", 1)[0]
        if flag in bypass or (source != "codex" and arg.startswith("-p") and not arg.startswith("--")):
            return False
        if flag in VALUES[source] and "=" not in arg:
            value_expected = "required"
        elif source == "claude" and arg in ("-r", "--resume"):
            value_expected = "optional"
        elif not arg.startswith("-") and not positional:
            if arg in COMMANDS[source]:
                return False
            positional = True
    return True


def launch_directory(source, args):
    directory = os.getcwd()
    if source == "codex":
        options = iter(args)
        for arg in options:
            if arg == "--":
                break
            if arg in ("-C", "--cd"):
                directory = next(options, directory)
            elif arg.startswith("--cd="):
                directory = arg[5:]
            elif arg.startswith("-C") and len(arg) > 2:
                directory = arg[2:].removeprefix("=")
            elif arg in VALUES["codex"]:
                next(options, None)
    return os.path.abspath(directory)


def launch(source, args):
    executable = shutil.which(source)
    if executable is None:
        print(f"session-ref: PATH 中找不到 {source}", file=sys.stderr)
        return 127
    argv = [executable, *args]
    if not all(os.isatty(i) for i in (0, 1, 2)) or not interactive(source, args):
        os.execv(executable, argv)
    trigger = os.environ.get("SESSION_REF_TRIGGER", " #")
    key = trigger.removeprefix(" ")
    if len(key) != 1 or not 33 <= ord(key) <= 126:
        print("session-ref: SESSION_REF_TRIGGER 需要一个可见 ASCII 字符，前面可加一个半角空格", file=sys.stderr)
        return 2
    directory = launch_directory(source, args)
    return run_pty(argv, trigger.encode(), lambda: Picker(ROOT / "session-ref", directory),
                   source=source, directory=directory)


def listing(sources, stream, summary, by_project=False):
    rows = {}
    errors = {}

    def receive(event):
        source = event["source"]
        if stream:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        else:
            for row in event.get("rows", []):
                rows[(source, row["id"])] = row
        if event.get("error"):
            errors[source] = event["error"]

    asyncio.run(collect(sources, receive))
    if not stream:
        if summary:
            for source in sources:
                print(source, sum(s == source for s, _ in rows), sep="\t")
        else:
            previous = None
            for row in recent_sessions(rows.values(), by_project=by_project):
                if by_project:
                    group = project_root(row["project"])
                    if group != previous:
                        print(f"[{clean(group) or '未知项目'}]")
                        previous = group
                date = datetime.fromtimestamp(row["updated"]).astimezone().isoformat(timespec="seconds") if row["updated"] else "—"
                print(row["source"], date, display_title(row), row["project"], sep="\t")
        for source, message in errors.items():
            print(f"{source}: {message}", file=sys.stderr)
    return int(bool(errors))


def main():
    if sys.argv[1:2] == ["_stream"]:
        return listing(SOURCES, True, False)
    # Preserve even a literal "--" and unknown options for the vendor's parser.
    if sys.argv[1:2] == ["run"] and sys.argv[2:3] and sys.argv[2] in SOURCES:
        return launch(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(prog="session-ref", description="跨 CLI 会话引用")
    subs = parser.add_subparsers(dest="command", required=True)
    run = subs.add_parser("run", help="启动交互式 CLI")
    run.add_argument("source", choices=SOURCES)
    run.add_argument("args", nargs=argparse.REMAINDER)
    ls = subs.add_parser("list", help="列出本机会话")
    ls.add_argument("--source", choices=SOURCES)
    view = ls.add_mutually_exclusive_group()
    view.add_argument("--summary", action="store_true", help="仅显示每个来源的会话数")
    view.add_argument("--by-project", action="store_true", help="按项目分组，再按更新时间排列")
    args = parser.parse_args()
    if args.command == "run":
        return launch(args.source, args.args)
    return listing((args.source,) if args.source else SOURCES, False, args.summary, args.by_project)
