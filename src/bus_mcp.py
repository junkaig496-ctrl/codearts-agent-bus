"""
agent-bus 的 MCP stdio 服务端（零依赖，手写 JSON-RPC 2.0）
=======================================================
码道 IDE / 码道 CLI / VS Code 插件 / JetBrains 插件都可以通过 stdio 拉起本进程。
每个码道会话会各自拉起一份，进程之间不共享内存，共享状态全在 bus_core 落盘层。

为什么手写而不装 SDK：码道要求"无需任何配置"的轻量接法，纯标准库 = 拷过去就能跑，
不用 pip install，也不会和码道的 Node/Python 环境打架。

MCP stdio 的帧格式是**每行一个 JSON**（不是 Content-Length），实现要点：
  initialize → notifications/initialized → tools/list → tools/call ...
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_core as core  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agent-bus"
SERVER_VERSION = core.BUS_VERSION

# ------------------------------------------------------------------ 工具定义

def _t(name, desc, props=None, required=None, **kw):
    schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": desc, "inputSchema": schema, **kw}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}

TOOLS = [
    _t("bus_register",
       "注册（或续用）你在总线上的会话身份。当你需要与其他码道会话/智能体协作时，"
       "第一步就调用它：给本会话起一个明确的角色名。同一个 name+project 会得到同一个 session_id，"
       "重复调用只是刷新心跳。Use when: 开始一个需要多会话协作的任务、或想让别的智能体找到你。",
       {"name": {"type": "string", "description": "本会话的角色名，唯一且见名知义，例如 planner / worker-frontend / reviewer"},
        "role": {"type": "string", "description": "角色：leader（派活与汇总）/ worker（执行）/ reviewer（审查）。默认 worker"},
        "project": {"type": "string", "description": "项目路径或标识，默认取当前工作目录；同一项目内同名即同一身份"},
        "capabilities": {"type": "string", "description": "我能干什么，便于别人派活给你，例如 前端React/样式/文档"},
        "notes": {"type": "string", "description": "补充说明，例如当前正在做什么"}},
       ["name"]),

    _t("bus_whoami",
       "查看自己的身份、未读消息数、在线同伴数。Use when: 不确定自己是否已注册、或想快速看是否有新消息。"),

    _t("bus_list_peers",
       "列出总线上有哪些会话（同伴发现，对标 Claude Code 未发布的 ListPeersTool）。"
       "Use when: 需要知道有谁在线、谁能干哪类活、或派活前确认目标名字。",
       {"include_offline": {**B, "description": "是否包含离线会话（默认 false，只看在线的）"}}),

    _t("bus_send",
       "给指定会话发一条消息。Use when: 需要通知、追问、传上下文给某个同伴。",
       {"to": {"type": "string", "description": "目标会话的 name 或 session_id"},
        "content": {**S, "description": "消息正文（可含结论、文件路径、下一步要求）"},
        "kind": {"type": "string", "description": "消息类型：note / question / status / context，默认 note"},
        "correlation_id": {**S, "description": "可选，关联的任务号，便于串起一条线索"}},
       ["to", "content"]),

    _t("bus_broadcast",
       "给所有在线会话广播一条消息。Use when: 宣布约定、让所有 worker 更新规则。",
       {"content": S, "kind": S}, ["content"]),

    _t("bus_inbox",
       "取自己的收件箱。wait_seconds>0 时进入**长轮询**（阻塞等待直到有新消息或超时）——"
       "这是 worker 会话待命领活的标准姿势。Use when: 每完成一步就来看有没有新指令；"
       "或作为 worker 时用 bus_inbox(wait_seconds=60) 待命。",
       {"wait_seconds": {**N, "description": "等待新消息的最长秒数，0=立即返回（默认 0）"},
        "limit": {**I, "description": "本次最多取几条，默认 20"},
        "ack": {**B, "description": "是否把取到的消息标记为已读，默认 true；peek 时用 false"}}),

    _t("bus_reply",
       "回复某条收到的消息（自动带上关联线索）。Use when: 收到 question 类消息后作答。",
       {"message_id": {**S, "description": "要回复的消息 msg_id"},
        "content": S}, ["message_id", "content"]),

    _t("bus_dispatch",
       "派发一个子任务给某个会话（或公共池），任务带状态机与租约。"
       "Use when: 作为 leader 把大任务拆成可并行的子任务。派给具体会话时会自动给它发一条消息。",
       {"title": {**S, "description": "任务标题（一句话说清交付物）"},
        "instructions": {**S, "description": "任务说明：要做什么、验收标准、涉及文件、约束"},
        "to": {**S, "description": "执行者 name/session_id；留空则进公共池，谁先领谁做"},
        "priority": {"type": "string", "description": "normal / high，默认 normal"},
        "timeout_seconds": {**I, "description": "租约秒数，执行者超时未回执则自动退回 pending，默认 300"},
        "artifacts": {**S, "description": "期望产物（文件路径等），便于回执核对"}},
       ["title", "instructions"]),

    _t("bus_next_task",
       "领取一个待办任务（优先指派给自己的，其次公共池），可长轮询等待。"
       "Use when: 作为 worker 主动找活干，或待命 bus_next_task(wait_seconds=60)。",
       {"wait_seconds": {**N, "description": "等待任务的最长秒数，默认 0"},
        "claim": {**B, "description": "是否同时认领（置为 running），默认 true"}}),

    _t("bus_progress",
       "给任务上报进度（长任务中途留痕，防止被租约回收）。Use when: 任务耗时较长。",
       {"task_id": S, "note": S, "percent": I}, ["task_id"]),

    _t("bus_report",
       "提交任务回执（done / failed / cancelled），会自动通知派活方。"
       "Use when: worker 完成任务后必须调用，否则派活方一直在等。",
       {"task_id": S, "status": {"type": "string", "description": "done / failed / cancelled"},
        "result": {**S, "description": "结果摘要（结论优先，别贴大段代码）"},
        "artifacts": {**S, "description": "产物路径，例如 C:/proj/index.html"}},
       ["task_id", "status"]),

    _t("bus_list_tasks",
       "查看任务看板（状态、负责人、结果）。Use when: leader 汇总前核对；或不确定还有谁没回执。",
       {"status": {**S, "description": "只看某状态：pending/running/done/failed/cancelled"},
        "mine": {**B, "description": "只看与我相关的任务"},
        "limit": {**I, "description": "最多返回条数，默认 50"}}),

    _t("bus_cancel_task",
       "取消一个任务。Use when: 任务已无意义或执行者失联。",
       {"task_id": S, "reason": S}, ["task_id"]),

    _t("bus_ps",
       "会话守护视图（对标 Claude Code 未发布的 Daemon Mode：像 docker ps 一样看待 AI 会话）："
       "谁在线、在干什么、几条未读、挂了几个任务。Use when: 汇报进度、排查谁没在干活。"),

    _t("bus_log",
       "总线事件流水（注册/派活/认领/回执/消息）。Use when: 复盘协作过程、写报告取证。",
       {"limit": {**I, "description": "默认 50"}, "kind": {**S, "description": "按事件或消息类型过滤"}}),

    _t("bus_dashboard",
       "生成/刷新本地协作看板（自包含单页 HTML，含会话拓扑与消息流），"
       "演示与答辩用。Use when: 需要给人看多会话协作的实时画面。",
       {"open": {**B, "description": "生成后用系统默认浏览器打开，默认 false"}}),

    _t("bus_leave",
       "下线本会话。Use when: 协作结束、任务全部完成。",
       {"note": S}),
]

TOOL_MAP = {t["name"]: t for t in TOOLS}

# ------------------------------------------------------------------ 会话绑定

class Session:
    """本 MCP 进程代表的码道会话（内存态；进程重启后需重新 register）。"""

    sid: str = ""

    @classmethod
    def bind(cls, sid: str) -> None:
        cls.sid = sid

    @classmethod
    def current(cls) -> str:
        """没注册过就自动兜底注册，避免智能体卡在"未注册"错误上空转。"""
        if cls.sid:
            return cls.sid
        name = os.environ.get("AGENT_BUS_NAME") or f"{Path(os.getcwd()).name}-{os.getpid() % 1000}"
        role = os.environ.get("AGENT_BUS_ROLE") or "worker"
        r = core.register(name=name, role=role, project=os.getcwd(),
                          notes="自动注册（未显式调用 bus_register）")
        cls.sid = r["session_id"]
        return cls.sid


# ------------------------------------------------------------------ 工具分发

def call_tool(name: str, args: dict) -> dict:
    args = args or {}

    if name == "bus_register":
        r = core.register(name=args.get("name", ""), role=args.get("role", "worker"),
                          project=args.get("project", ""),
                          capabilities=args.get("capabilities", ""),
                          notes=args.get("notes", ""))
        Session.bind(r["session_id"])
        r["hint"] = ("身份已就绪。接下来：leader 用 bus_list_peers + bus_dispatch 派活；"
                     "worker 用 bus_next_task / bus_inbox 领活。")
        return r

    sid = Session.current()

    if name == "bus_whoami":
        return core.whoami(sid)
    if name == "bus_list_peers":
        return core.list_peers(bool(args.get("include_offline")), sid=sid)
    if name == "bus_send":
        return core.send(sid, args["to"], args["content"], args.get("kind", "note"),
                         args.get("correlation_id", ""))
    if name == "bus_broadcast":
        return core.broadcast(sid, args["content"], args.get("kind", "note"))
    if name == "bus_inbox":
        return core.inbox(sid, args.get("wait_seconds", 0), args.get("limit", 20),
                          args.get("ack", True))
    if name == "bus_reply":
        return core.reply(sid, args["message_id"], args["content"])
    if name == "bus_dispatch":
        return core.dispatch(sid, args["title"], args.get("instructions", ""),
                             args.get("to", ""), args.get("priority", "normal"),
                             args.get("timeout_seconds", core.DEFAULT_LEASE),
                             args.get("artifacts", ""))
    if name == "bus_next_task":
        return core.next_task(sid, args.get("wait_seconds", 0), args.get("claim", True))
    if name == "bus_progress":
        return core.progress(sid, args["task_id"], args.get("note", ""), args.get("percent"))
    if name == "bus_report":
        return core.report(sid, args["task_id"], args.get("status", "done"),
                           args.get("result", ""), args.get("artifacts", ""))
    if name == "bus_list_tasks":
        return core.list_tasks(sid, args.get("status", ""), bool(args.get("mine")),
                               args.get("limit", 50))
    if name == "bus_cancel_task":
        return core.cancel_task(sid, args["task_id"], args.get("reason", ""))
    if name == "bus_ps":
        return core.ps(sid)
    if name == "bus_log":
        return core.log(sid, args.get("limit", 50), args.get("kind", ""))
    if name == "bus_dashboard":
        import bus_dashboard
        path = bus_dashboard.build()
        if args.get("open"):
            import webbrowser
            webbrowser.open(path.as_uri())
        return {"ok": True, "path": str(path), "uri": path.as_uri(),
                "hint": "看板是快照式的：每次调用都会用最新状态覆盖生成，刷新浏览器即可。"}
    if name == "bus_leave":
        return core.leave(sid, args.get("note", ""))

    raise core.BusError(f"未知工具：{name}")


# ------------------------------------------------------------------ JSON-RPC

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(mid, payload):
    _send({"jsonrpc": "2.0", "id": mid, "result": payload})


def _error(mid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": mid, "error": err})


def handle(msg: dict) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        want = params.get("protocolVersion") or PROTOCOL_VERSION
        _result(mid, {
            "protocolVersion": want,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": ("agent-bus：码道会话总线。多会话协作时，先 bus_register 起个角色名；"
                             "leader 用 bus_dispatch 派活，worker 用 bus_next_task / bus_inbox 领活，"
                             "完成后 bus_report 回执。等你明白了就照做，不要重复自我介绍。"),
        })
        return

    if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
        return

    if method == "ping":
        _result(mid, {})
        return

    if method == "tools/list":
        _result(mid, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_MAP:
            _result(mid, {"content": [{"type": "text",
                                       "text": f"未知工具 {name}。可用工具：{', '.join(TOOL_MAP)}"}],
                          "isError": True})
            return
        missing = [k for k in (TOOL_MAP[name]["inputSchema"].get("required") or [])
                   if args.get(k) in (None, "")]
        if missing:
            _result(mid, {"content": [{"type": "text",
                                       "text": f"缺少必填参数：{', '.join(missing)}。"
                                               f"请补齐后重试（可参考工具 {name} 的 inputSchema）。"}],
                          "isError": True})
            return
        try:
            payload = call_tool(name, args)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            _result(mid, {"content": [{"type": "text", "text": text}],
                          "structuredContent": payload, "isError": False})
        except core.BusError as e:
            _result(mid, {"content": [{"type": "text", "text": f"总线拒绝了这个操作：{e}"}],
                          "isError": True})
        except KeyError as e:
            _result(mid, {"content": [{"type": "text", "text": f"缺少必填参数：{e}"}],
                          "isError": True})
        except Exception as e:                                  # noqa: BLE001
            _result(mid, {"content": [{"type": "text",
                                       "text": f"总线内部错误：{e}\n{traceback.format_exc()[-800:]}"}],
                          "isError": True})
        return

    # 有些客户端会探测这些；返回空集合比报错更稳
    if method in ("resources/list", "resources/templates/list"):
        _result(mid, {"resources": [], "resourceTemplates": []})
        return
    if method == "prompts/list":
        _result(mid, {"prompts": []})
        return
    if method == "logging/setLevel":
        _result(mid, {})
        return

    if is_notification:
        return
    _error(mid, -32601, f"Method not found: {method}")


def main() -> int:
    core._ensure_dirs()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, list):                               # 批处理
            for one in msg:
                if isinstance(one, dict):
                    handle(one)
            continue
        if isinstance(msg, dict):
            handle(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
