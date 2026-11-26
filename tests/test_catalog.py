import asyncio
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from session_ref.catalog import Rpc, agy, claude_rows, codex, collect, pages, project_root, recent_sessions, record
from session_ref.cli import launch_directory


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_invalid_row_does_not_discard_other_sessions(self):
        client = AsyncMock()

        @contextlib.asynccontextmanager
        async def rpc(*_):
            yield client

        client.call.side_effect = [{}, {"data": [None, {"id": "bad id"}, {"id": "active"}]},
                                   {"data": [{"id": "archived"}]}]
        with patch("session_ref.catalog.rpc_process", rpc):
            rows = [r async for page in codex() for r in page]
            self.assertEqual([r["id"] for r in rows], ["active", "archived"])
            client.call.side_effect = [{"agentCapabilities": {"sessionCapabilities": {"list": {}}}},
                                       {"sessions": [{}, {"sessionId": "valid"}]}]
            self.assertEqual([r["id"] async for page in agy() for r in page], ["valid"])
            client.call.side_effect = [{"agentCapabilities": {"sessionCapabilities": None}}]
            with self.assertRaisesRegex(RuntimeError, "未提供 ACP session/list"):
                async for _ in agy():
                    pass

        sdk = SimpleNamespace(list_sessions=lambda: [None, SimpleNamespace(session_id="bad id"),
                              SimpleNamespace(session_id="old", last_modified=None),
                              SimpleNamespace(session_id="new", last_modified=32000, summary="summary")])
        with patch.dict(sys.modules, claude_agent_sdk=sdk):
            self.assertEqual([(r["id"], r["updated"], r["title"]) for r in claude_rows()],
                             [("old", 0, ""), ("new", 32, "summary")])

    async def test_rpc_ignores_banner_and_unrelated_json_before_response(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'banner\n\xff\ntrue\n[]\n{"id":0,"result":{}}\n{"id":1,"result":{"rows":[]}}\n')
        reader.feed_eof()
        client = Rpc(SimpleNamespace(stdout=reader, stdin=Mock(drain=AsyncMock())))
        self.assertEqual(await client.call("session/list", {}), {"rows": []})

    async def test_pagination_passes_opaque_cursor_and_keeps_filters(self):
        client = AsyncMock()
        client.call.side_effect = [{"rows": [1], "nextCursor": "opaque"}, {"rows": [2]}]
        actual = [r async for r in pages(client, "list", {"limit": 1}, "rows")]
        self.assertEqual(actual, [[1], [2]])
        client.call.assert_awaited_with("list", {"limit": 1, "cursor": "opaque"})

    async def test_repeated_cursor_terminates(self):
        client = AsyncMock()
        client.call.return_value = {"rows": [], "nextCursor": "same"}
        with self.assertRaisesRegex(ValueError, "游标"):
            async for _ in pages(client, "list", {}, "rows"):
                pass

    async def test_one_failed_source_keeps_other_results(self):
        async def good():
            yield [record("agy", "abc", None, "/project", "2026-11-26T01:00:00Z")]

        async def bad():
            raise TimeoutError()
            yield

        events = []
        with patch.dict("session_ref.catalog.PROVIDERS", agy=good, codex=bad):
            await collect(["agy", "codex"], events.append)
        self.assertGreater(events[0]["rows"][0]["updated"], 0)
        self.assertEqual(events[-1]["source"], "codex")
        self.assertIn("超时", events[-1]["error"])

    def test_untrusted_id_cannot_inject_keys(self):
        for identifier in ("a\r", "a\n", "a\x1b[1~", ""):
            with self.assertRaises(ValueError):
                record("codex", identifier, "", "", 0)

    def test_project_priority_before_quota_and_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "repo"
            (root / ".git").mkdir(parents=True)
            sub = root / "src"
            sub.mkdir()
            alias = root.parent / "alias"
            alias.symlink_to(sub, target_is_directory=True)
            nested = sub / "nested"
            nested.mkdir()
            (nested / ".git").touch()
            sibling = str(root) + "-tools"
            rows = [record("codex", f"x{i}", "", sibling, 100 + i) for i in range(6)]
            rows += [record("codex", "old", "", str(sub), 1), record("agy", "current", "", str(root), 2),
                     record("agy", "nested", "", str(nested), 103.5)]
            self.assertEqual(project_root(str(alias)), str(root))
            self.assertEqual(project_root(str(nested)), str(nested))
            self.assertEqual(project_root(sibling), sibling)
            self.assertEqual(launch_directory("codex", ["--model", "test", "--cd", str(alias)]), str(alias))
            self.assertEqual([r["id"] for r in recent_sessions(rows, str(alias))],
                             ["current", "old", "x5", "x4", "nested", "x3", "x2"])
            self.assertEqual([r["id"] for r in recent_sessions(rows, str(sub), by_project=True)],
                             ["current", "old", "x5", "x4", "x3", "x2", "nested"])
