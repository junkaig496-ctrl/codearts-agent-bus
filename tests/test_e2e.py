"""
agent-bus 端到端测试（不依赖 pytest，直接 python tests/test_e2e.py 跑）
====================================================================
覆盖三件事：
  1) 核心协作链路：注册 → 派活 → 收消息 → 领任务 → 进度 → 回执 → 汇总
  2) 跨进程：用子进程调 bus_cli，验证文件锁 + 原子写在多进程下不打架
  3) MCP 协议：真的把 bus_mcp.py 当子进程拉起来，走 stdio 发 JSON-RPC 握手，
     确认 tools/list 与 tools/call 正常（这层不对，码道里就全废了）

用临时 AGENT_BUS_HOME，不动你真实的 ~/.codeartsdoer/agent-bus。
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
TMP_HOME = Path(tempfile.mkdtemp(prefix="agent-bus-test-"))
os.environ["AGENT_BUS_HOME"] = str(TMP_HOME)
sys.path.insert(0, str(SRC))

import bus_core as core          # noqa: E402
import bus_dashboard             # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


def test_core_flow() -> None:
    print("\n[1] 核心协作链路")
    core.reset(confirm=True)

    planner = core.register(name="planner", role="leader", project="/tmp/demo-proj")
    worker = core.register(name="worker-1", role="worker", project="/tmp/demo-proj")
    check("注册两个会话", planner["session_id"] != worker["session_id"])

    # 同名同项目 → 同一身份（幂等），不应产生影子会话
    again = core.register(name="planner", role="leader", project="/tmp/demo-proj")
    check("同名同项目幂等注册", again["session_id"] == planner["session_id"]
          and again["registered"] is False)
    check("会话总数为 2", len(core.ps()["sessions"]) == 2)

    peers = core.list_peers(sid=planner["session_id"])
    check("同伴发现", peers["count"] == 2 and any(p["is_me"] for p in peers["peers"]))

    t = core.dispatch(planner["session_id"], "写首页", "产出 index.html", to="worker-1",
                      priority="high", artifacts="index.html")
    tid = t["task_id"]
    check("派活成功", t["ok"] and t["status"] == "pending")

    box = core.inbox(worker["session_id"])
    check("worker 收到派活消息", box["count"] == 1 and box["messages"][0]["kind"] == "task")
    check("消息带任务关联号", box["messages"][0]["correlation_id"] == tid)
    check("已读游标生效", core.inbox(worker["session_id"])["count"] == 0)

    got = core.next_task(worker["session_id"])
    check("worker 领到任务", bool(got["task"]) and got["task"]["task_id"] == tid)
    check("任务转为 running", core.list_tasks(planner["session_id"])["summary"].get("running") == 1)

    core.progress(worker["session_id"], tid, "骨架完成", percent=50)
    check("进度上报", core.list_tasks(planner["session_id"])["tasks"][0]["percent"] == 50)

    core.report(worker["session_id"], tid, "done", result="index.html 已产出",
                artifacts="index.html")
    summary = core.list_tasks(planner["session_id"])["summary"]
    check("任务完成", summary.get("done") == 1)

    recv = core.inbox(planner["session_id"])
    kinds = [m["kind"] for m in recv["messages"]]
    check("planner 收到回执通知", "task_report" in kinds)

    # 公共池 + 高优先级优先
    core.dispatch(planner["session_id"], "公共任务", "谁都行", to="")
    got2 = core.next_task(worker["session_id"])
    check("公共池可被认领", bool(got2["task"]) and got2["task"]["to_name"] == "（公共池）")

    # 长轮询：无消息时应等满再返回
    t0 = time.time()
    wait_res = core.inbox(planner["session_id"], wait_seconds=1.0)
    dt = time.time() - t0
    check("长轮询按等待时长阻塞", dt >= 0.9 and wait_res["count"] == 0, f"等了 {dt:.2f}s")

    # 定向消息 + 回复
    m = core.send(planner["session_id"], "worker-1", "请顺手补一个 README", kind="question")
    got_msg = core.inbox(worker["session_id"])
    check("定向消息送达", got_msg["count"] == 1)
    rep = core.reply(worker["session_id"], m["msg_id"], "收到，10 分钟内给")
    back = core.inbox(planner["session_id"])
    check("回复回到原发送方", any(x["kind"] == "reply" for x in back["messages"]),
          f"reply_to={rep['reply_to']}")

    # 租约回收：派一个 lease=0 的任务，worker 认领后超时，下一次领活时应被退回 pending
    short = core.dispatch(planner["session_id"], "会被回收的任务", "长任务", to="worker-1",
                          timeout_seconds=0)
    claimed = core.next_task(worker["session_id"])
    check("lease=0 的任务已被认领（避免默认值把它顶掉）",
          bool(claimed.get("task")) and claimed["task"]["task_id"] == short["task_id"],
          str(claimed.get("task", {}).get("task_id")))
    time.sleep(1.1)
    reclaimed = core.next_task(worker["session_id"])
    check("租约超时任务被回收", short["task_id"] in (reclaimed.get("reclaimed") or []),
          str(reclaimed.get("reclaimed")))

    # 广播 / 守护视图 / 统计
    b = core.broadcast(planner["session_id"], "约定：产物统一放 dist/")
    check("广播送达在线会话", b["delivered"] == 1)
    psv = core.ps()
    check("守护视图含未读与活跃任务", "unread" in psv["sessions"][0] and "tasks" in psv)
    check("事件/消息流水非空", core.log(limit=99)["count"] > 5)

    # 离线判定
    core.leave(worker["session_id"])
    check("下线后不再出现在在线同伴里",
          len(core.list_peers(sid=planner["session_id"])["peers"]) == 1)

    # 看板生成
    d = bus_dashboard.build()
    body = d.read_text(encoding="utf-8")
    check("看板 HTML 生成", d.exists() and "agent-bus" in body and "会话拓扑" in body)
    check("看板自包含（无外链）", "http://" not in body.split("<body")[0].replace("http://www.w3.org", ""))
    titles = [t["title"] for t in core.list_tasks(status="", limit=99)["tasks"]]
    missing = [tt for tt in titles if tt not in body]
    check("看板涵盖全部任务（不是只渲染一条）", titles and not missing,
          f"{len(titles)} 个任务，缺 {missing}")


def test_cross_process() -> None:
    print("\n[2] 跨进程（文件锁 + 原子写 + 并发）")
    env = dict(os.environ, AGENT_BUS_HOME=str(TMP_HOME), PYTHONIOENCODING="utf-8")
    cli = str(SRC / "bus_cli.py")

    def run(*args: str) -> dict:
        r = subprocess.run([sys.executable, cli, *args], capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=90)
        if r.returncode != 0:
            raise AssertionError(f"bus_cli {' '.join(args)} 失败：{(r.stderr or r.stdout)[-400:]}")
        return json.loads(r.stdout)

    def run_concurrent(n: int, make_args) -> None:
        procs = [subprocess.Popen([sys.executable, cli, *make_args(i)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, encoding="utf-8")
                 for i in range(n)]
        errs = []
        for p in procs:
            _, e = p.communicate(timeout=90)
            if p.returncode != 0:
                errs.append((e or "")[-200:])
        check(f"{n} 个并发进程都正常退出", not errs, str(errs[:2]))

    core.reset(confirm=True)
    run("--as", "proc-a", "register", "proc-a", "--role", "leader")
    run("--as", "proc-b", "register", "proc-b", "--role", "worker")

    run_concurrent(6, lambda i: ("--as", "proc-a", "dispatch", f"并发任务 {i}", "压测",
                                 "--to", "proc-b"))
    tasks = run("tasks")
    check("并发派发 6 个任务无丢失", tasks["count"] == 6, f"实际 {tasks['count']}")

    run_concurrent(6, lambda i: ("--as", "proc-a", "send", "proc-b", f"并发消息 {i}"))
    ps = run("ps")
    # 6 条派活通知 + 6 条显式消息 = 12；锁不住就会少（丢写）或多（重复写）
    check("并发写无丢失/无重复（6 通知 + 6 消息 = 12）",
          ps["messages_total"] == 12, f"实际 {ps['messages_total']}")
    check("收件方真的收到 12 条",
          run("--as", "proc-b", "inbox", "-n", "50")["count"] == 12)

    core.reset(confirm=True)


def test_mcp_stdio() -> None:
    print("\n[3] MCP stdio 协议握手（码道真正走的这条链路）")
    env = dict(os.environ, AGENT_BUS_HOME=str(TMP_HOME), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", str(SRC / "bus_mcp.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)

    def rpc(obj: dict, expect: bool = True) -> dict | None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        if not expect:
            return None
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("MCP 服务器没有响应（可能崩溃了）：" + (proc.stderr.read() or "")[-400:])
        return json.loads(line)

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18",
                           "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
    check("initialize 返回 serverInfo",
          init["result"]["serverInfo"]["name"] == "agent-bus", str(init["result"]["serverInfo"]))
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)

    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [t["name"] for t in tools["result"]["tools"]]
    need = ["bus_register", "bus_list_peers", "bus_send", "bus_inbox", "bus_dispatch",
            "bus_next_task", "bus_report", "bus_ps", "bus_dashboard"]
    check("tools/list 暴露全部关键工具",
          all(n in names for n in need), f"{len(names)} 个工具")
    check("每个工具都有 inputSchema",
          all("inputSchema" in t and t["inputSchema"].get("type") == "object"
              for t in tools["result"]["tools"]))

    reg = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "bus_register",
                          "arguments": {"name": "mcp-session-a", "role": "leader"}}})
    payload = json.loads(reg["result"]["content"][0]["text"])
    check("tools/call 注册成功", payload.get("session_id", "").startswith("mcp-session-a"))
    check("返回同时带 structuredContent", reg["result"].get("structuredContent", {}).get("ok") is not False)

    reg2 = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "bus_list_peers", "arguments": {}}})
    peers = json.loads(reg2["result"]["content"][0]["text"])
    check("同一进程内身份被记住（能查同伴）", peers["count"] >= 1)

    bad = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": "bus_dispatch", "arguments": {"title": "x"}}})
    check("缺必填参数返回 isError", bad["result"]["isError"] is True
          and "instructions" in bad["result"]["content"][0]["text"],
          bad["result"]["content"][0]["text"][:40])

    bad2 = rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "bus_send", "arguments": {"to": "nobody"}}})
    check("缺 content 也拦得住", bad2["result"]["isError"] is True)

    unknown = rpc({"jsonrpc": "2.0", "id": 6, "method": "no/such/method", "params": {}})
    check("未知方法返回 -32601", unknown.get("error", {}).get("code") == -32601)

    ping = rpc({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
    check("ping 正常", ping.get("result") == {})

    proc.stdin.close()
    proc.wait(timeout=20)
    check("MCP 进程能干净退出", proc.returncode == 0)
    core.reset(confirm=True)


def test_concurrent_threads() -> None:
    """回归测试：同进程多线程"边读边替换"（码道的智能体写并发测试时暴露的 Windows 坑）。

    Windows 上 os.replace 不允许目标文件被别的句柄打开，所以"一个线程在读 registry.json、
    另一个线程正好在 os.replace 它"会抛 PermissionError(13)。修复方式是短暂退避重试。
    这里刻意把读与写拉满，任何未捕获的异常都会让用例失败。
    """
    print("\n[4] 同进程并发读写（Windows 文件替换竞争回归）")
    import threading

    core.reset(confirm=True)
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                core.ps()
                core.list_tasks()          # 只读视图，传空 sid
                core.log(limit=5)
                core.list_peers(include_offline=True)
            except Exception as e:          # noqa: BLE001
                errors.append(f"reader: {type(e).__name__}: {e}")

    def writer(n: int):
        try:
            for i in range(12):
                sid = core.register(name=f"t{n}", role="worker", project=f"/tmp/th{n}",
                                    notes=f"第 {i} 轮")["session_id"]
                if i % 3 == 0:              # 顺带制造 tasks.jsonl 的并发追加
                    core.dispatch(sid, f"并发任务 {n}-{i}", "压测", to="")
        except Exception as e:              # noqa: BLE001
            errors.append(f"writer{n}: {type(e).__name__}: {e}")

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=60)
    stop.set()
    for t in readers:
        t.join(timeout=10)

    check("并发读写无异常（PermissionError 已消除）", not errors,
          str(errors[:3]))
    check("写操作全部落盘（4 个 writer × 12 轮注册共 4 个会话）",
          len(core.ps()["sessions"]) == 4, f"实际 {len(core.ps()['sessions'])}")
    core.reset(confirm=True)


if __name__ == "__main__":
    print(f"临时总线目录：{TMP_HOME}")
    test_core_flow()
    test_cross_process()
    test_mcp_stdio()
    test_concurrent_threads()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败 → {FAILED}")
        sys.exit(1)
    print("结果：全部通过 ✅  （agent-bus 可以接进码道了）")
