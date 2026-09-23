"""
bus_core 单元测试（纯标准库 unittest，不引入 pytest）
=====================================================
覆盖五个必测点：
  1) 同名同项目注册幂等（再次注册=刷新心跳，不产生影子会话）
  2) 消息已读游标（inbox 取一次后推进游标，不再重复返回）
  3) 任务状态机 dispatch→claim→report 折叠（事件流正确折叠为终态）
  4) 租约超时回收（running 任务超过 timeout_seconds 被退回 pending）
  5) 原子写不产生半截 JSON（_write_json_atomic 整体替换、无残留临时文件）

环境隔离：所有用例共用项目内固定目录 .ab-test-home 作为 AGENT_BUS_HOME，
setUp/tearDown 只清空该目录下的文件（不创建目录），绝不污染真实
~/.codeartsdoer/agent-bus。

注：本机沙箱禁止 msvcrt/fcntl 文件锁与目录创建，而五个必测点关注的业务逻辑
（注册幂等/游标/状态机/租约/原子写）在单进程下与文件锁互斥无关，
故 setUp 用空 contextmanager 替换 bus_core._lock 以专注测业务逻辑；
跨进程文件锁的正确性由 tests/test_e2e.py（子进程）覆盖。

运行：
    python -m unittest tests.test_bus_core_unit -v
    python tests/test_bus_core_unit.py
    python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import bus_core as core  # noqa: E402

TEST_HOME = Path(__file__).resolve().parent.parent / ".ab-test-home"


def _clean_home(home: Path) -> None:
    for sub in (home, home / "inbox", home / "cursors"):
        if sub.is_dir():
            for p in sub.iterdir():
                if p.is_file():
                    with contextlib.suppress(FileNotFoundError):
                        p.unlink()


class BusCoreTestBase(unittest.TestCase):
    """所有用例共用固定 TEST_HOME，setUp 清空状态、tearDown 清场。"""

    def setUp(self) -> None:
        self._saved_home = core.STATE_HOME
        self._saved_lock = core._TOOLS_LOCK
        self._saved_local = core._LOCAL
        self._saved_lock_fn = core._lock
        core.STATE_HOME = TEST_HOME
        core._TOOLS_LOCK = None
        core._LOCAL = None

        @contextlib.contextmanager
        def _noop_lock(timeout: float = 10.0):
            yield
        core._lock = _noop_lock

        _clean_home(TEST_HOME)
        core._ensure_dirs()

    def tearDown(self) -> None:
        core.STATE_HOME = self._saved_home
        core._TOOLS_LOCK = self._saved_lock
        core._LOCAL = self._saved_local
        core._lock = self._saved_lock_fn
        _clean_home(TEST_HOME)

    def _proj(self) -> str:
        return str(TEST_HOME / "demo-proj")


class TestRegisterIdempotent(BusCoreTestBase):
    """① 同名同项目注册幂等。"""

    def test_same_name_project_returns_same_sid_and_heartbeat(self) -> None:
        proj = self._proj()
        first = core.register(name="planner", role="leader", project=proj,
                              capabilities="拆解")
        second = core.register(name="planner", role="leader", project=proj,
                               capabilities="拆解")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertTrue(first["registered"], "首次注册 registered 应为 True")
        self.assertFalse(second["registered"], "再次注册 registered 应为 False（心跳）")
        self.assertEqual(first["peers"], 1)
        self.assertEqual(second["peers"], 1, "幂等不应新增会话")

    def test_different_name_or_project_yields_different_sid(self) -> None:
        proj = self._proj()
        a = core.register(name="planner", role="leader", project=proj)
        b = core.register(name="worker", role="worker", project=proj)
        c = core.register(name="planner", role="leader", project=str(TEST_HOME / "other"))
        self.assertNotEqual(a["session_id"], b["session_id"], "不同 name 应不同 sid")
        self.assertNotEqual(a["session_id"], c["session_id"], "不同 project 应不同 sid")
        self.assertEqual(len(core.ps()["sessions"]), 3)


class TestInboxCursor(BusCoreTestBase):
    """② 消息已读游标：取一次后不再重复返回。"""

    def test_inbox_advances_cursor_and_not_repeat(self) -> None:
        proj = self._proj()
        a = core.register(name="alice", role="worker", project=proj)
        b = core.register(name="bob", role="worker", project=proj)
        core.send(a["session_id"], "bob", "第一条")
        core.send(a["session_id"], "bob", "第二条")

        first = core.inbox(b["session_id"])
        self.assertEqual(first["count"], 2, "首次应取到全部 2 条")
        self.assertTrue(first["acked"], "默认 ack 应推进游标")
        self.assertEqual({m["content"] for m in first["messages"]},
                         {"第一条", "第二条"})

        second = core.inbox(b["session_id"])
        self.assertEqual(second["count"], 0, "游标推进后不应重复返回")
        self.assertEqual(second["unread_before"], 0)

    def test_inbox_without_ack_keeps_cursor(self) -> None:
        proj = self._proj()
        a = core.register(name="alice", role="worker", project=proj)
        b = core.register(name="bob", role="worker", project=proj)
        core.send(a["session_id"], "bob", "只读不推进")
        peek = core.inbox(b["session_id"], ack=False)
        self.assertEqual(peek["count"], 1)
        self.assertFalse(peek["acked"])
        again = core.inbox(b["session_id"])
        self.assertEqual(again["count"], 1, "未 ack 时游标不推进，仍可再取")


class TestTaskStateMachine(BusCoreTestBase):
    """③ 任务状态机 dispatch→claim→report 折叠。"""

    def test_dispatch_claim_report_folds_to_done(self) -> None:
        proj = self._proj()
        leader = core.register(name="leader", role="leader", project=proj)
        worker = core.register(name="worker", role="worker", project=proj)

        d = core.dispatch(leader["session_id"], "写首页", "产出 index.html",
                          to="worker", priority="high", artifacts="index.html")
        tid = d["task_id"]
        self.assertEqual(d["status"], "pending")

        nt = core.next_task(worker["session_id"], claim=True)
        self.assertIsNotNone(nt["task"])
        self.assertEqual(nt["task"]["task_id"], tid)
        self.assertEqual(nt["task"]["status"], "running", "claim 后应为 running")

        r = core.report(worker["session_id"], tid, status="done",
                        result="index.html 已生成", artifacts="index.html")
        self.assertEqual(r["status"], "done")

        rows = core.list_tasks()["tasks"]
        row = next(t for t in rows if t["task_id"] == tid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"], "index.html 已生成")

    def test_report_failed_status_preserved(self) -> None:
        proj = self._proj()
        leader = core.register(name="leader", role="leader", project=proj)
        worker = core.register(name="worker", role="worker", project=proj)
        tid = core.dispatch(leader["session_id"], "失败样例", "x",
                            to="worker")["task_id"]
        core.next_task(worker["session_id"], claim=True)
        core.report(worker["session_id"], tid, status="failed", result="卡住了")
        row = next(t for t in core.list_tasks()["tasks"] if t["task_id"] == tid)
        self.assertEqual(row["status"], "failed")

    def test_public_pool_task_claimable_by_anyone(self) -> None:
        proj = self._proj()
        leader = core.register(name="leader", role="leader", project=proj)
        worker = core.register(name="worker", role="worker", project=proj)
        tid = core.dispatch(leader["session_id"], "公共池活", "谁空闲谁领")["task_id"]
        nt = core.next_task(worker["session_id"], claim=True)
        self.assertIsNotNone(nt["task"])
        self.assertEqual(nt["task"]["task_id"], tid)


class TestLeaseReclaim(BusCoreTestBase):
    """④ 租约超时回收：running 任务超过 timeout_seconds 退回 pending。"""

    def test_lease_timeout_reclaims_to_pending(self) -> None:
        proj = self._proj()
        leader = core.register(name="leader", role="leader", project=proj)
        worker = core.register(name="worker", role="worker", project=proj)
        tid = core.dispatch(leader["session_id"], "短租约活", "做Y",
                            to="worker", timeout_seconds=1)["task_id"]
        nt = core.next_task(worker["session_id"], claim=True)
        self.assertEqual(nt["task"]["status"], "running")

        time.sleep(1.5)
        rows = core.list_tasks()["tasks"]
        row = next(t for t in rows if t["task_id"] == tid)
        self.assertEqual(row["status"], "pending", "超租约应被回收为 pending")

        reclaimed = core.next_task(worker["session_id"], wait_seconds=0)
        self.assertIn(tid, reclaimed["reclaimed"], "reclaimed 列表应含该任务")

    def test_within_lease_not_reclaimed(self) -> None:
        proj = self._proj()
        leader = core.register(name="leader", role="leader", project=proj)
        worker = core.register(name="worker", role="worker", project=proj)
        tid = core.dispatch(leader["session_id"], "长租约活", "做Z",
                            to="worker", timeout_seconds=10)["task_id"]
        core.next_task(worker["session_id"], claim=True)
        time.sleep(0.3)
        row = next(t for t in core.list_tasks()["tasks"] if t["task_id"] == tid)
        self.assertEqual(row["status"], "running", "未超租约应保持 running")


class TestAtomicWrite(BusCoreTestBase):
    """⑤ 原子写不产生半截 JSON。"""

    def test_roundtrip_preserves_content(self) -> None:
        path = TEST_HOME / "data.json"
        obj = {"中文": "ok", "nested": {"a": 1, "b": [1, 2, 3]}, "n": None, "f": 3.14}
        core._write_json_atomic(path, obj)
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertEqual(loaded, obj)

    def test_overwrite_replaces_not_appends(self) -> None:
        path = TEST_HOME / "data.json"
        core._write_json_atomic(path, {"v": 1, "big": "x" * 500})
        core._write_json_atomic(path, {"v": 2})
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertEqual(loaded, {"v": 2}, "二次写应整体替换，而非追加拼接")

    def test_no_leftover_tmp_files(self) -> None:
        path = TEST_HOME / "data.json"
        for i in range(5):
            core._write_json_atomic(path, {"i": i})
        leftovers = list(TEST_HOME.glob(".tmp-*.json"))
        self.assertEqual(leftovers, [], "原子写完成后不应残留临时文件")

    def test_concurrent_writers_all_leave_valid_json(self) -> None:
        import threading
        path = TEST_HOME / "shared.json"
        errors: list[Exception] = []

        def writer(n: int) -> None:
            try:
                for i in range(20):
                    core._write_json_atomic(path, {"writer": n, "i": i})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], "并发写不应抛异常")
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertIn("writer", loaded, "并发后文件仍是合法 JSON 且内容完整")


if __name__ == "__main__":
    unittest.main(verbosity=2)
