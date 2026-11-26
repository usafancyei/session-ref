"""List session metadata through the three providers' supported interfaces."""

import asyncio
import contextlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from heapq import nsmallest
from pathlib import Path


SOURCES = ("codex", "claude", "agy")
DISPLAY_LIMIT_PER_SOURCE = 5
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


@lru_cache(maxsize=2048)
def project_root(directory):
    if not os.path.isabs(directory):
        return directory
    path = Path(os.path.realpath(directory))
    for parent in (path, *path.parents):
        try:
            if (parent / ".git").exists():
                return str(parent)
        except OSError:
            break
    return str(path)


def display_title(row):
    title = row["title"]
    return title if title and title != row["id"] else "（无标题）"


def recent_sessions(rows, directory=None, *, by_project=False):
    current = project_root(directory or os.getcwd())
    by_source = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row)

    def priority(row):
        return (project_root(row["project"]) != current, row["updated"] == 0,
                -row["updated"], row["source"], row["id"])

    recent = []
    for group in by_source.values():
        recent.extend(nsmallest(DISPLAY_LIMIT_PER_SOURCE, group, key=priority))
    ordered = sorted(recent, key=priority)
    if not by_project:
        return ordered
    # First occurrence is the project's newest visible session.
    projects = defaultdict(list)
    for row in ordered:
        projects[project_root(row["project"])].append(row)
    return [row for group in projects.values() for row in group]


def clean(value):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())


def valid_id(value):
    return isinstance(value, str) and SAFE_ID.fullmatch(value) is not None


def record(source, identifier, title, project, timestamp, *, milliseconds=False):
    # Only this field can reach a terminal input stream.
    if not valid_id(identifier):
        raise ValueError("会话 ID 格式无效")
    try:
        if milliseconds:
            timestamp = float(timestamp) / 1000
        elif isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        timestamp = float(timestamp)
        # Both list views format in the local timezone.
        datetime.fromtimestamp(timestamp)
    except (TypeError, ValueError, OverflowError, OSError):
        timestamp = 0
    return dict(source=source, id=identifier, title=clean(title)[:240],
                project=clean(project), updated=timestamp)


class Rpc:
    def __init__(self, process):
        self.process = process
        self.sequence = 0

    async def send(self, message):
        self.process.stdin.write(json.dumps(dict(jsonrpc="2.0", **message)).encode() + b"\n")
        await self.process.stdin.drain()

    async def call(self, method, params):
        self.sequence += 1
        await self.send(dict(id=self.sequence, method=method, params=params))
        async with asyncio.timeout(12):
            while line := await self.process.stdout.readline():
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(reply, dict):
                    continue
                if "method" in reply:
                    if "id" in reply:
                        await self.send(dict(id=reply["id"], error=dict(
                            code=-32601, message="Unsupported client method")))
                    continue
                if reply.get("id") == self.sequence:
                    if "error" in reply:
                        code = reply["error"].get("code")
                        raise RuntimeError(f"{method} 返回错误 {code}")
                    return reply["result"]
        raise RuntimeError(f"{method} 响应前连接关闭")


@contextlib.asynccontextmanager
async def rpc_process(*args):
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
    try:
        yield Rpc(proc)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), 1)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.communicate()


async def pages(client, method, params, key):
    seen = set()
    while True:
        page = await client.call(method, params)
        yield page[key]
        cursor = page.get("nextCursor")
        if not cursor:
            return
        if cursor in seen:
            raise ValueError("分页游标重复")
        seen.add(cursor)
        params = dict(params, cursor=cursor)


async def codex():
    async with rpc_process("codex", "app-server") as client:
        await client.call("initialize", {"clientInfo": {
            "name": "session_ref", "title": "Session Ref", "version": "1"}})
        await client.send({"method": "initialized"})
        params = dict(sortKey="updated_at", limit=100, modelProviders=[],
                      sourceKinds=["cli", "vscode", "appServer"])
        for archived in (False, True):
            async for rows in pages(client, "thread/list", dict(params, archived=archived), "data"):
                yield [record("codex", r["id"], r.get("name") or r.get("preview"),
                              r.get("cwd"), r.get("updatedAt")) for r in rows
                       if isinstance(r, dict) and valid_id(r.get("id"))]


async def agy():
    async with rpc_process("agy", "acp") as client:
        info = await client.call("initialize", dict(protocolVersion=1, clientCapabilities={}))
        caps = (info.get("agentCapabilities") or {}).get("sessionCapabilities")
        if not isinstance(caps, dict) or caps.get("list") in (None, False):
            raise RuntimeError("此版本未提供 ACP session/list")
        async for rows in pages(client, "session/list", {}, "sessions"):
            yield [record("agy", r["sessionId"], r.get("title"), r.get("cwd"),
                          r.get("updatedAt")) for r in rows
                   if isinstance(r, dict) and valid_id(r.get("sessionId"))]


def claude_rows():
    from claude_agent_sdk import list_sessions

    return [record("claude", r.session_id, getattr(r, "custom_title", None) or getattr(r, "summary", None),
                   getattr(r, "cwd", None), getattr(r, "last_modified", None), milliseconds=True)
            for r in list_sessions() if valid_id(getattr(r, "session_id", None))]


async def claude():
    yield await asyncio.to_thread(claude_rows)


PROVIDERS = {"codex": codex, "agy": agy, "claude": claude}


async def collect(sources, emit):
    async def load(source):
        try:
            async for rows in PROVIDERS[source]():
                emit(dict(source=source, rows=rows))
        except FileNotFoundError:
            error = "找不到启动命令"
        except ImportError:
            error = "Claude SDK 加载失败，请运行 make setup"
        except TimeoutError:
            error = "会话接口超时"
        except RuntimeError as exc:
            error = str(exc)
        except Exception as exc:
            # Diagnostics must not echo session data or provider subprocess output.
            error = f"读取会话失败 ({type(exc).__name__})"
        else:
            error = ""
        emit(dict(source=source, done=True, error=error))

    await asyncio.gather(*(load(source) for source in sources))
