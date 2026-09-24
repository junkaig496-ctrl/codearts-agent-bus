"""
双会话集成测试：用两个独立的 MCP 进程模拟码道的两个会话
=====================================================
test_e2e.py 验证的是"组件对不对"；这个脚本验证的是**真实场景**：
两个各自独立的 MCP 子进程（就像码道为两个会话分别拉起的两份），
只通过总线通信，完成一次完整的「派活 → 收活 → 执行 → 回执 → 汇总」。

它等价于真机演示的最小复现，因此也可以当作"码道不在手边时的演示脚本"。

    python tests/test_two_sessions.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
TMP_HOME = Path(tempfile.mkdtemp(prefix="agent-bus-2sess-"))
ENV = dict(os.environ, AGENT_BUS_HOME=str(TMP_HOME), PYTHONIOENCODING="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


class Session:
    """一个码道会话 = 一个 bus_mcp.py 子进程 + 一条 stdio 管线。"""

    def __init__(self, label: str):
        self.label = label
        self.proc = subprocess.Popen([sys.executable, "-u", str(SRC / "bus_mcp.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     env=ENV)
        self._id = 0
        self.rpc({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": label, "version": "0"}}})
        self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self) -> int:
        self._id += 1
        return self._id

    def rpc(self, obj: dict) -> dict:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"{self.label} 无响应：{(self.proc.stderr.read() or '')[-300:]}")
        return json.loads(line)

    def notify(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, tool: str, **args) -> dict:
        """调用工具，返回解析后的业务负载。"""
        r = self.rpc({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                      "params": {"name": tool, "arguments": args}})
        res = r.get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "{}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"_raw": text}
        payload["_is_error"] = bool(res.get("isError"))
        return payload

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=20)


def main() -> int:
    print(f"临时总线目录：{TMP_HOME}")
    print("\n[双会话集成] 两个独立 MCP 进程 = 码道的两个会话\n")

    a = Session("session-A(planner)")
    b = Session("session-B(worker)")

    # ---- 1. 各自报名
    ra = a.call("bus_register", name="planner", role="leader", capabilities="拆解/汇总")
    rb = b.call("bus_register", name="worker-frontend", role="worker", capabilities="HTML/CSS")
    check("会话 A 注册成功", str(ra.get("session_id", "")).startswith("planner-"), ra.get("session_id", ""))
    check("会话 B 注册成功", str(rb.get("session_id", "")).startswith("worker-frontend-"),
          rb.get("session_id", ""))
    check("两个会话 ID 不同", ra.get("session_id") != rb.get("session_id"))

    # ---- 2. A 能"看见" B（参考：同伴发现）
    peers = a.call("bus_list_peers")
    names = [p["name"] for p in peers.get("peers", [])]
    check("A 发现同伴 B", "worker-frontend" in names, str(names))

    # ---- 3. A 派活给 B
    t = a.call("bus_dispatch", title="实现 index.html 首页骨架",
               instructions="产出单文件 HTML，只允许改 index.html，验收：浏览器打开无报错",
               to="worker-frontend", priority="high", artifacts="index.html")
    check("A 派活成功", t.get("ok") is True and str(t.get("task_id", "")).startswith("t_"),
          t.get("task_id", ""))
    tid = t["task_id"]

    # ---- 4. B 收信（长轮询）+ 领活
    box = b.call("bus_inbox", wait_seconds=5)
    got = [m for m in box.get("messages", []) if m.get("kind") == "task"]
    check("B 通过总线收到派活消息", len(got) == 1, f"{len(got)} 条")
    check("消息携带任务号（可串线索）", bool(got) and got[0].get("correlation_id") == tid)

    task = b.call("bus_next_task", wait_seconds=5)
    check("B 领到任务", (task.get("task") or {}).get("task_id") == tid,
          str((task.get("task") or {}).get("task_id")))

    # ---- 5. B 上报进度 + 回执
    pr = b.call("bus_progress", task_id=tid, note="骨架完成，正在补样式", percent=60)
    check("B 上报进度成功", pr.get("ok") is True and pr.get("percent") == 60)

    rp = b.call("bus_report", task_id=tid, status="done",
                result="index.html 已生成，含标题区与任务列表区，浏览器打开无报错",
                artifacts="index.html")
    check("B 提交回执成功", rp.get("ok") is True and rp.get("status") == "done")

    # ---- 6. A 收到回执并汇总
    abox = a.call("bus_inbox", wait_seconds=5)
    receipts = [m for m in abox.get("messages", []) if str(m.get("kind", "")).startswith("task")]
    check("A 收到任务回执", len(receipts) == 1, f"{len(receipts)} 条")
    check("回执内容含结果与产物",
          bool(receipts) and "index.html" in receipts[0].get("content", ""))

    tasks = a.call("bus_list_tasks")
    check("任务看板显示已完成", (tasks.get("summary") or {}).get("done") == 1,
          str(tasks.get("summary")))

    # ---- 7. 守护视图 + 看板（参考：会话守护视图）
    psv = a.call("bus_ps")
    check("守护视图能看到两个在线会话", len(psv.get("sessions", [])) == 2,
          str([(s["name"], s["state"]) for s in psv.get("sessions", [])]))
    dash = a.call("bus_dashboard")
    dp = Path(dash.get("path", ""))
    check("看板生成成功", dp.exists() and dp.stat().st_size > 2000,
          f"{dp.stat().st_size if dp.exists() else 0} 字节")
    body = dp.read_text(encoding="utf-8") if dp.exists() else ""
    check("看板含两个会话与任务", "planner" in body and "worker-frontend" in body
          and tid in body)

    # ---- 8. 广播 + 下线
    bc = a.call("bus_broadcast", content="约定：产物统一放 dist/")
    check("A 广播成功", bc.get("delivered") == 1, str(bc.get("delivered")))
    lv = b.call("bus_leave")
    check("B 下线成功", lv.get("state") == "offline")
    peers_after = a.call("bus_list_peers")
    check("下线后 A 的同伴列表里不再有 B", peers_after.get("count") == 1,
          str([p["name"] for p in peers_after.get("peers", [])]))

    a.close()
    b.close()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败 → {FAILED}")
        return 1
    print("结果：双会话全链路通过 ✅ —— 两个会话真的通过总线完成了协作")
    return 0


if __name__ == "__main__":
    sys.exit(main())
