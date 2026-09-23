"""
agent-bus 命令行（给"人"用的入口，也是演示脚本）
==============================================
智能体走 MCP（bus_mcp.py）；你自己在终端里看状态、手动发消息就用这个 CLI。

常用：
    python bus_cli.py ps                 # 会话守护视图（谁在线、在干什么）
    python bus_cli.py log -n 30          # 事件流水
    python bus_cli.py demo               # 一键跑完整的两会话协作剧本
    python bus_cli.py dashboard --open   # 生成并打开看板
    python bus_cli.py mcp                # 以 stdio MCP 服务器方式启动（供码道拉起）
    python bus_cli.py http --port 8765   # 启动 StreamableHTTP 端点（给 Space/远程）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_core as core   # noqa: E402

CLI_SESSION_FILE = core.home() / "cli-session.json"


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def my_session(name: str = "", role: str = "leader", as_name: str = "") -> str:
    """CLI 自己的身份。

    ``as_name`` 指定时只做幂等注册、**不写** cli-session.json —— 这样同一台机器上
    可以用 ``--as planner`` / ``--as worker-1`` 并行扮演多个会话（压测与演示都用得上）。
    """
    if as_name:
        return core.register(name=as_name, role=role, project=os.getcwd(),
                             notes="CLI 指定身份")["session_id"]
    if name:
        r = core.register(name=name, role=role, project=os.getcwd(), notes="CLI 手动会话")
        CLI_SESSION_FILE.write_text(json.dumps({"session_id": r["session_id"], "name": r["name"]}),
                                    encoding="utf-8")
        return r["session_id"]
    try:
        sid = json.loads(CLI_SESSION_FILE.read_text(encoding="utf-8"))["session_id"]
        core._touch(sid)
        return sid
    except Exception:                                        # noqa: BLE001
        return my_session("cli-operator", role="leader")


# --------------------------------------------------------------------- 演示剧本

def demo(wait: float = 1.0, quiet: bool = False) -> dict:
    """完整剧本：leader 拆任务 → 派给两个 worker → worker 领活/上报 → leader 汇总。

    这个函数既是端到端自检，也是答辩演示脚本（不依赖码道也能跑，方便复现）。
    """
    steps: list[str] = []

    def say(msg: str):
        steps.append(msg)
        if not quiet:
            print(f"  ▸ {msg}")

    print("=" * 68)
    print(" agent-bus 演示：两个码道会话协作完成一次「拆解 → 执行 → 汇总」")
    print("=" * 68)

    planner = core.register(name="planner", role="leader", project=os.getcwd(),
                            capabilities="需求拆解/验收/汇总")
    say(f"leader 上线：planner = {planner['session_id']}")

    fe = core.register(name="worker-frontend", role="worker", project=os.getcwd(),
                       capabilities="HTML/CSS 页面实现")
    be = core.register(name="worker-docs", role="worker", project=os.getcwd(),
                       capabilities="中文文档撰写")
    say(f"两个 worker 上线：worker-frontend / worker-docs")

    peers = core.list_peers(sid=planner["session_id"])
    say(f"planner 发现同伴 {peers['count']} 个")

    t1 = core.dispatch(planner["session_id"],
                       title="实现 index.html 首页骨架",
                       instructions="产出单文件 HTML，含标题区/任务列表区；验收：浏览器能打开且无报错",
                       to="worker-frontend", priority="high", artifacts="index.html")
    t2 = core.dispatch(planner["session_id"],
                       title="撰写项目说明文档",
                       instructions="产出 docs/说明.md，含安装步骤与演示流程",
                       to="worker-docs", artifacts="docs/说明.md")
    say(f"planner 派发 2 个任务：{t1['task_id']} / {t2['task_id']}")

    # worker-frontend 先"听见"派活消息，再领任务
    box = core.inbox(fe["session_id"], wait_seconds=0.2)
    say(f"worker-frontend 收件箱收到 {box['count']} 条：{(box['messages'][0]['content'][:38] if box['count'] else '')}…")

    got = core.next_task(fe["session_id"], wait_seconds=1.0)
    if not got.get("task"):
        raise AssertionError("worker-frontend 没领到任务")
    say(f"worker-frontend 领到任务：{got['task']['task_id']} 「{got['task']['title']}」")

    core.progress(fe["session_id"], t1["task_id"], "骨架已完成，正在补样式", percent=60)
    say("worker-frontend 上报进度 60%")

    core.report(fe["session_id"], t1["task_id"], "done",
                result="index.html 已生成，含标题区与任务列表区，浏览器打开无报错",
                artifacts="index.html")
    say("worker-frontend 提交回执 done")

    # planner 收到回执
    box2 = core.inbox(planner["session_id"], wait_seconds=0.2)
    receipts = [m for m in box2["messages"] if m["kind"].startswith("task")]
    say(f"planner 收到回执 {len(receipts)} 条：{(receipts[0]['content'][:40] if receipts else '')}…")

    # 公共池任务：谁空闲谁领
    core.dispatch(planner["session_id"], title="公共池：整理验收清单",
                  instructions="列出验收要点 5 条", to="", priority="normal")
    got2 = core.next_task(be["session_id"], wait_seconds=1.0)
    say(f"worker-docs 从公共池领到：{got2['task']['title'] if got2.get('task') else '(无)'}")

    tasks = core.list_tasks(planner["session_id"])
    stats = core.stats()
    say(f"任务总览：{tasks['summary']}；总线统计：会话 {stats['sessions_total']} / 消息 {stats['messages_total']}")

    dash = core.home() / "dashboard.html"
    import bus_dashboard
    p = bus_dashboard.build(out=dash)
    say(f"看板已生成：{p}")

    print("-" * 68)
    print(f" 剧本跑通：{len(steps)} 步，无异常。")
    return {"steps": steps, "tasks": tasks["summary"], "stats": stats, "dashboard": str(p)}


# --------------------------------------------------------------------- 子命令

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bus_cli", description="agent-bus 命令行 / 演示工具")
    ap.add_argument("--as", dest="as_name", default="",
                    help="以指定会话身份执行（例如 --as planner），可并行扮演多个会话")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("register", help="注册本 CLI 会话")
    p.add_argument("name", nargs="?", default="cli-operator")
    p.add_argument("--role", default="leader")

    sub.add_parser("whoami", help="我的身份与未读数")
    sub.add_parser("ps", help="会话守护视图")

    p = sub.add_parser("peers", help="同伴列表")
    p.add_argument("--all", action="store_true", help="包含离线会话")

    p = sub.add_parser("log", help="事件流水")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--kind", default="")

    p = sub.add_parser("send", help="给某会话发消息")
    p.add_argument("to")
    p.add_argument("content")
    p.add_argument("--kind", default="note")

    p = sub.add_parser("broadcast", help="广播")
    p.add_argument("content")

    p = sub.add_parser("inbox", help="看自己的收件箱")
    p.add_argument("-w", "--wait", type=float, default=0)
    p.add_argument("-n", type=int, default=20)

    p = sub.add_parser("reply", help="回复消息")
    p.add_argument("message_id")
    p.add_argument("content")

    p = sub.add_parser("dispatch", help="派任务")
    p.add_argument("title")
    p.add_argument("instructions")
    p.add_argument("--to", default="")
    p.add_argument("--priority", default="normal")

    p = sub.add_parser("next", help="领任务")
    p.add_argument("-w", "--wait", type=float, default=0)

    p = sub.add_parser("tasks", help="任务看板")
    p.add_argument("--status", default="")
    p.add_argument("--mine", action="store_true")

    p = sub.add_parser("report", help="提交回执")
    p.add_argument("task_id")
    p.add_argument("--status", default="done", choices=["done", "failed", "cancelled"])
    p.add_argument("--result", default="")
    p.add_argument("--artifacts", default="")

    p = sub.add_parser("dashboard", help="生成看板")
    p.add_argument("--open", action="store_true")

    p = sub.add_parser("demo", help="跑一遍完整的两会话协作剧本")
    p.add_argument("--fast", action="store_true", help="减少等待时间")

    p = sub.add_parser("reset", help="清空总线数据（演示前重置）")
    p.add_argument("--yes", action="store_true")

    sub.add_parser("mcp", help="以 stdio MCP 服务器启动（码道通过 stdio 拉起）")
    p = sub.add_parser("http", help="启动 StreamableHTTP/SSE MCP 端点")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("selftest", help="快速自检 core 层")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)

    def me() -> str:
        return my_session(as_name=getattr(a, "as_name", ""))

    if a.cmd in (None, "ps"):
        out(core.ps() if a.cmd else {"hint": "用法：python bus_cli.py ps | demo | dashboard | mcp | http"})
        return 0

    if a.cmd == "register":
        sid = my_session(a.name, a.role, as_name=getattr(a, "as_name", ""))
        out({"session_id": sid})
    elif a.cmd == "whoami":
        out(core.whoami(me()))
    elif a.cmd == "peers":
        out(core.list_peers(a.all))
    elif a.cmd == "log":
        out(core.log(limit=a.n, kind=a.kind))
    elif a.cmd == "send":
        out(core.send(me(), a.to, a.content, kind=a.kind))
    elif a.cmd == "broadcast":
        out(core.broadcast(me(), a.content))
    elif a.cmd == "inbox":
        out(core.inbox(me(), wait_seconds=a.wait, limit=a.n))
    elif a.cmd == "reply":
        out(core.reply(me(), a.message_id, a.content))
    elif a.cmd == "dispatch":
        out(core.dispatch(me(), a.title, a.instructions, to=a.to, priority=a.priority))
    elif a.cmd == "next":
        out(core.next_task(me(), wait_seconds=a.wait))
    elif a.cmd == "tasks":
        out(core.list_tasks(me(), a.status, a.mine))
    elif a.cmd == "report":
        out(core.report(me(), a.task_id, a.status, a.result, a.artifacts))
    elif a.cmd == "dashboard":
        import bus_dashboard
        p = bus_dashboard.build(open_after=a.open)
        out({"dashboard": str(p), "uri": p.as_uri()})
    elif a.cmd == "demo":
        res = demo(wait=0.2 if a.fast else 1.0)
        out({"demo": "ok", "tasks": res["tasks"], "dashboard": res["dashboard"]})
    elif a.cmd == "reset":
        out(core.reset(confirm=a.yes))
    elif a.cmd == "mcp":
        import bus_mcp
        return bus_mcp.main()
    elif a.cmd == "http":
        import bus_http
        bus_http.serve(a.host, a.port)
    elif a.cmd == "selftest":
        sid = core.register(name="selftest", role="worker")
        out({"register": sid, "stats": core.stats()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
