"""
agent-bus 核心层（纯标准库，跨平台）
=====================================
给「华为云码道（CodeArts）代码智能体」补上官方缺失的能力：**会话之间的通信与协作**。

设计要点（对应泄露报告里的 UDS Inbox / Daemon Mode）
-------------------------------------------------
* 每个码道会话各自拉起一个 MCP 子进程，进程间不共享内存 —— 所以共享状态必须落盘。
* 状态根目录默认 ``~/.codeartsdoer/agent-bus``（沿用码道自己的 .codeartsdoer 约定），
  可用环境变量 ``AGENT_BUS_HOME`` 覆盖。
* 所有"读-改-写"操作走同一把文件锁；写文件用「临时文件 + os.replace」保证原子性。
* 消息队列是 append-only 的 JSONL（每会话一个 inbox），读游标单独存，
  这样即使两个会话同时收发也不会互相踩。
* 任务用事件流（tasks.jsonl）表达，状态由事件折叠得出，带 lease 租约防"领了不干"。

对外只暴露函数，传输层（stdio / StreamableHTTP）在 bus_mcp.py、bus_http.py 里。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------- 常量与路径

DEFAULT_HOME = Path(os.path.expanduser("~")) / ".codeartsdoer" / "agent-bus"
STATE_HOME = Path(os.environ.get("AGENT_BUS_HOME") or DEFAULT_HOME)

ONLINE_TTL = 90.0          # 心跳超过这个秒数视为离线
DEFAULT_LEASE = 300        # 认领任务后的租约（秒），超时自动回到 pending
BUS_VERSION = "1.0.0"

_TOOLS_LOCK = None         # 进程内线程锁（同进程多线程安全）
_LOCAL = None              # threading.local：记录每线程的加锁深度（可重入）


def _thread_local():
    global _LOCAL
    import threading
    if _LOCAL is None:
        _LOCAL = threading.local()
    return _LOCAL


def home() -> Path:
    return STATE_HOME


def _p(*parts: str) -> Path:
    return STATE_HOME.joinpath(*parts)


@contextlib.contextmanager
def _lock(timeout: float = 10.0):
    """跨进程文件锁，**可重入**。

    两个平台坑必须同时躲开：
      1. Windows 的 ``msvcrt.locking`` 是强制锁，且**同一进程用不同句柄锁同一字节也会被自己挡住**，
         所以嵌套加锁必须靠"深度计数"短路，而不是靠再开一个句柄。
      2. 多线程（HTTP 传输层）下，真正的文件锁只允许最外层那一个线程持有。
    """
    global _TOOLS_LOCK
    import threading

    if _TOOLS_LOCK is None:
        _TOOLS_LOCK = threading.RLock()

    _ensure_dirs()
    loc = _thread_local()
    depth = getattr(loc, "depth", 0)

    if depth > 0:                       # 本线程已持有 → 只记深度，直接放行
        loc.depth = depth + 1
        try:
            yield
        finally:
            loc.depth -= 1
        return

    lock_path = _p("bus.lock")
    got_mutex = _TOOLS_LOCK.acquire(timeout=timeout)
    if not got_mutex:
        raise BusError("bus.lock 获取超时：另一个线程正长时间占用总线")
    fh = None
    try:
        fh = open(lock_path, "a+b")
        if os.path.getsize(lock_path) == 0:      # 某些平台要求锁区在文件内
            fh.write(b"0")
            fh.flush()
        acquired = False
        deadline = time.time() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise BusError("bus.lock 获取超时：另一个进程正长时间占用总线")
                time.sleep(0.05)
        loc.depth = 1
        try:
            yield
        finally:
            loc.depth = 0
            if acquired:
                with contextlib.suppress(Exception):
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if fh is not None:
            fh.close()
        _TOOLS_LOCK.release()


def _ensure_dirs() -> None:
    _p("inbox").mkdir(parents=True, exist_ok=True)
    _p("cursors").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 基础工具

class BusError(Exception):
    """业务错误：会以 isError=true 返回给智能体，让它自己纠错。"""


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else now()))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _read_json(path: Path, default):
    """读 JSON，带**短暂重试**。

    Windows 上如果此刻另一个线程/进程正在对同一路径做 ``os.replace``（原子写），
    打开文件可能瞬时被拒（PermissionError 13）——重试几十毫秒即可，不该让调用方看见这个抖动。
    这个坑是码道自己的智能体写并发测试时暴露出来的（见 docs/02 第八节）。
    """
    deadline = time.time() + 2.0
    while True:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            return default
        except PermissionError:
            if time.time() >= deadline:
                return default
            time.sleep(0.02)


def _write_json_atomic(path: Path, obj) -> None:
    """原子写：临时文件 + fsync + os.replace，**并对 Windows 的替换竞争做重试**。"""
    _ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        deadline = time.time() + 3.0
        while True:
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # 目标文件正被别的线程/进程打开（Windows 上 replace 不允许），退避后重试
                if time.time() >= deadline:
                    raise BusError(
                        f"写入 {path.name} 失败：目标文件被其他进程长时间占用（Windows 文件替换竞争）")
                time.sleep(0.02)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def _append_jsonl(path: Path, obj) -> None:
    _ensure_dirs()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "-", (name or "").strip()).strip("-")
    return (s or "session")[:32]


def _sh6(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:6]


# ---------------------------------------------------------------- 会话注册表

def _reg_path() -> Path:
    return _p("registry.json")


def _load_registry() -> dict:
    reg = _read_json(_reg_path(), {"sessions": {}, "version": BUS_VERSION})
    reg.setdefault("sessions", {})
    return reg


def _status_of(sess: dict, ts: float | None = None) -> str:
    ts = ts if ts is not None else now()
    if not sess:
        return "unknown"
    if ts - float(sess.get("last_seen", 0)) > ONLINE_TTL:
        return "offline"
    return sess.get("state") or "idle"


def register(name: str, role: str = "worker", project: str = "", capabilities: str = "",
             notes: str = "", session_id: str = "") -> dict:
    """注册（或续用）一个会话身份。

    同一个 ``name`` + ``project`` 会得到同一个 session_id，因此智能体在同一个项目里
    反复调用本工具只会"刷新心跳"，不会产生一堆影子会话。
    """
    if not name:
        raise BusError("name 不能为空：给这个会话起个唯一名字，例如 planner / worker-frontend")

    project = project or os.getcwd()
    sid = session_id or f"{_slug(name)}-{_sh6(os.path.abspath(project))}"
    ts = now()

    with _lock():
        reg = _load_registry()
        sess = reg["sessions"].get(sid, {})
        first = not sess
        sess.update({
            "session_id": sid,
            "name": _slug(name),
            "role": role or sess.get("role") or "worker",
            "project": os.path.abspath(project),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "capabilities": capabilities or sess.get("capabilities", ""),
            "notes": notes or sess.get("notes", ""),
            "state": sess.get("state") or "idle",
            "last_seen": ts,
            "first_seen": sess.get("first_seen", ts),
            "host": os.environ.get("COMPUTERNAME") or os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
        })
        reg["sessions"][sid] = sess
        _write_json_atomic(_reg_path(), reg)
        _append_jsonl(_p("events.jsonl"), {
            "ts": ts, "ts_iso": iso(ts), "event": "register" if first else "heartbeat",
            "session_id": sid, "name": sess["name"], "role": sess["role"],
        })

    return {"session_id": sid, "registered": first, "name": sess["name"],
            "role": sess["role"], "project": sess["project"], "peers": len(reg["sessions"])}


def _touch(sid: str, state: str | None = None, required: bool = True) -> dict:
    """刷新心跳。``required=False`` 时允许空 sid（给看板/只读视图用）。"""
    if not sid:
        if required:
            raise BusError("本会话尚未注册：请先调用 bus_register 起一个明确的角色名")
        return {}
    with _lock():
        reg = _load_registry()
        sess = reg["sessions"].get(sid)
        if not sess:
            raise BusError(f"会话 {sid} 未注册，请先调用 bus_register")
        sess["last_seen"] = now()
        if state:
            sess["state"] = state
        _write_json_atomic(_reg_path(), reg)
        return sess


def whoami(sid: str) -> dict:
    with _lock():
        reg = _load_registry()
        sess = reg["sessions"].get(sid)
        if not sess:
            raise BusError(f"会话 {sid} 未注册，请先调用 bus_register")
        sess["last_seen"] = now()
        _write_json_atomic(_reg_path(), reg)
        peers = [s for k, s in reg["sessions"].items() if k != sid]
        return {
            "session_id": sid, "name": sess["name"], "role": sess["role"],
            "project": sess["project"], "state": sess.get("state"),
            "unread": _unread_count(sid),
            "peers_online": sum(1 for s in peers if _status_of(s) != "offline"),
            "peers_total": len(peers),
            "bus_home": str(STATE_HOME),
            "bus_version": BUS_VERSION,
        }


def list_peers(include_offline: bool = False, sid: str = "") -> dict:
    ts = now()
    with _lock():
        reg = _load_registry()
    rows = []
    for k, s in reg["sessions"].items():
        st = _status_of(s, ts)
        if st == "offline" and not include_offline:
            continue
        rows.append({
            "session_id": k, "name": s.get("name"), "role": s.get("role"),
            "state": st, "capabilities": s.get("capabilities", ""),
            "notes": s.get("notes", ""), "project": s.get("project", ""),
            "last_seen": iso(s.get("last_seen")), "is_me": k == sid,
            "age_seconds": round(ts - float(s.get("last_seen", 0)), 1),
        })
    rows.sort(key=lambda r: (not r["is_me"], r["name"] or ""))
    return {"count": len(rows), "peers": rows}


# ---------------------------------------------------------------- 消息

def _cursors() -> dict:
    return _read_json(_p("cursors.json"), {})


def _save_cursors(cur: dict) -> None:
    _write_json_atomic(_p("cursors.json"), cur)


def _inbox_path(sid: str) -> Path:
    return _p("inbox", f"{sid}.jsonl")


def _unread_count(sid: str) -> int:
    total = len(_read_jsonl(_inbox_path(sid)))
    cur = int(_cursors().get(sid, {}).get("inbox", 0))
    return max(0, total - cur)


def _resolve(sid: str, target: str) -> str:
    """把 name / session_id / 前缀 解析成唯一 session_id。"""
    with _lock():
        reg = _load_registry()
    sess = reg["sessions"]
    if target in sess:
        return target
    cands = [k for k, v in sess.items() if v.get("name") == target]
    if not cands:
        cands = [k for k in sess if k.startswith(target)]
    if not cands:
        raise BusError(f"找不到会话「{target}」。用 bus_list_peers 看看现在有谁在线")
    if len(cands) > 1:
        raise BusError(f"「{target}」匹配到多个会话：{cands}。请用完整 session_id")
    return cands[0]


def _deliver(msg: dict) -> None:
    _append_jsonl(_inbox_path(msg["to"]), msg)
    _append_jsonl(_p("messages.jsonl"), {**msg, "delivery": "inbox"})


def send(sid: str, to: str, content: str, kind: str = "note",
         correlation_id: str = "", reply_to: str = "") -> dict:
    if not content:
        raise BusError("content 不能为空")
    target = _resolve(sid, to)
    # 允许给自己发消息（相当于给同伴留个"便利贴"，也方便单会话自测），不做拦截
    ts = now()
    msg = {
        "msg_id": _new_id("m"), "ts": ts, "ts_iso": iso(ts),
        "from": sid, "from_name": _name_of(sid), "to": target, "to_name": _name_of(target),
        "kind": kind or "note", "content": content,
        "correlation_id": correlation_id or "", "reply_to": reply_to or "",
    }
    with _lock():
        _touch(sid, state="working")
        _deliver(msg)
    return {"ok": True, "msg_id": msg["msg_id"], "to": target,
            "to_name": msg["to_name"], "at": msg["ts_iso"]}


def broadcast(sid: str, content: str, kind: str = "note") -> dict:
    if not content:
        raise BusError("content 不能为空")
    with _lock():
        reg = _load_registry()
        targets = [k for k, s in reg["sessions"].items()
                   if k != sid and _status_of(s) != "offline"]
    got = []
    for t in targets:
        send(sid, t, content, kind=kind)
        got.append(t)
    return {"ok": True, "delivered": len(got), "to": got}


def _name_of(sid: str) -> str:
    reg = _load_registry()
    return (reg["sessions"].get(sid) or {}).get("name") or sid


def inbox(sid: str, wait_seconds: float = 0, limit: int = 20, ack: bool = True) -> dict:
    """取收件箱。

    ``wait_seconds > 0`` 时进入**长轮询**：直到有新消息或超时才返回。
    这是让 worker 会话"能听见派活"的关键 —— MCP 只能被智能体主动调用，
    所以 worker 端要养成 ``bus_inbox(wait_seconds=60)`` 待命的习惯。
    """
    wait_seconds = max(0.0, float(wait_seconds or 0))
    deadline = now() + wait_seconds

    while True:
        with _lock():
            _touch(sid)
            msgs = _read_jsonl(_inbox_path(sid))
            cur = int(_cursors().get(sid, {}).get("inbox", 0))
            new = msgs[cur:]
            if new or now() >= deadline or wait_seconds == 0:
                if ack and new:
                    cu = _cursors()
                    cu.setdefault(sid, {})["inbox"] = cur + len(new)
                    _save_cursors(cu)
                picked = new[: max(1, int(limit or 20))]
                return {
                    "count": len(picked), "unread_before": len(new),
                    "acked": bool(ack and new), "waited_seconds": round(
                        max(0.0, wait_seconds - max(0.0, deadline - now())), 1),
                    "messages": picked,
                    "hint": "" if new else "暂无新消息。若在等派活，可继续调用 bus_inbox(wait_seconds=60) 待命。",
                }
        time.sleep(0.25)


def reply(sid: str, message_id: str, content: str) -> dict:
    with _lock():
        all_msgs = _read_jsonl(_p("messages.jsonl"))
    origin = next((m for m in all_msgs if m.get("msg_id") == message_id), None)
    if not origin:
        raise BusError(f"找不到消息 {message_id}")
    target = origin.get("from")
    if not target or target == sid:
        raise BusError("这条消息不需要回复（或目标就是自己）")
    res = send(sid, target, content, kind="reply",
               correlation_id=origin.get("correlation_id") or message_id,
               reply_to=message_id)
    res["reply_to"] = message_id
    return res


# ---------------------------------------------------------------- 任务（含租约）

def _fold_tasks() -> dict:
    """把 tasks.jsonl 事件流折叠成 {task_id: task}。"""
    tasks: dict[str, dict] = {}
    for ev in _read_jsonl(_p("tasks.jsonl")):
        tid = ev.get("task_id")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"task_id": tid, "history": []})
        t["history"].append({"event": ev.get("event"), "ts_iso": ev.get("ts_iso"),
                             "by_name": ev.get("by_name")})
        for k in ("title", "instructions", "priority", "from", "from_name", "to",
                  "to_name", "timeout_seconds", "result", "artifacts", "progress_note",
                  "percent", "by", "by_name"):
            if k in ev and ev[k] is not None:
                t[k] = ev[k]
        t["status"] = ev.get("status") or t.get("status") or "pending"
        t["updated_ts"] = ev.get("ts", t.get("updated_ts", 0))
    return tasks


def _lease_expired(t: dict, ts: float | None = None) -> bool:
    """任务是否已超出认领租约（**纯判断，不写盘**，供只读视图使用）。"""
    if t.get("status") not in ("claimed", "running"):
        return False
    lease = float(DEFAULT_LEASE if t.get("timeout_seconds") is None else t["timeout_seconds"])
    base = float(t.get("updated_ts") or t.get("dispatched_ts") or 0)
    return (ts if ts is not None else now()) - base > lease


def _reclaim_leases(tasks: dict) -> list[str]:
    """把超出租约的 running 任务退回 pending（对标"领了不干"的防呆）。

    只在**写路径**（``next_task``）里调用；``list_tasks`` 是纯读视图，不产生副作用。
    """
    reclaimed = []
    ts = now()
    for t in tasks.values():
        if _lease_expired(t, ts):
            _append_jsonl(_p("tasks.jsonl"), {
                "ts": ts, "ts_iso": iso(ts), "event": "lease_expired", "task_id": t["task_id"],
                "status": "pending", "by": "bus", "by_name": "bus",
                "result": f"租约超时（{int(float(DEFAULT_LEASE if t.get('timeout_seconds') is None else t['timeout_seconds']))}s）自动退回 pending",
            })
            t["status"] = "pending"
            t["updated_ts"] = ts
            reclaimed.append(t["task_id"])
    return reclaimed


def dispatch(sid: str, title: str, instructions: str, to: str = "",
             priority: str = "normal", timeout_seconds: int = DEFAULT_LEASE,
             artifacts: str = "") -> dict:
    if not title:
        raise BusError("title 不能为空")
    ts = now()
    tid = _new_id("t")
    target = ""
    if to:
        target = _resolve(sid, to)
    ev = {
        "ts": ts, "ts_iso": iso(ts), "event": "dispatch", "task_id": tid,
        "status": "pending", "title": title, "instructions": instructions or "",
        "priority": priority or "normal",
        "timeout_seconds": int(DEFAULT_LEASE if timeout_seconds is None else timeout_seconds),
        "from": sid, "from_name": _name_of(sid), "to": target,
        "to_name": _name_of(target) if target else "", "artifacts": artifacts or "",
    }
    with _lock():
        _touch(sid, state="working")
        _append_jsonl(_p("tasks.jsonl"), ev)
        if target:
            send(sid, target, f"【新任务 {tid}】{title}\n\n{instructions or ''}".strip(),
                 kind="task", correlation_id=tid)
    return {"ok": True, "task_id": tid, "status": "pending", "assigned_to": target or "（待认领）",
            "at": ev["ts_iso"]}


def next_task(sid: str, wait_seconds: float = 0, claim: bool = True) -> dict:
    """worker 领任务：优先取指派给自己的 pending，其次取公共池。"""
    deadline = now() + max(0.0, float(wait_seconds or 0))
    while True:
        with _lock():
            _touch(sid)
            tasks = _fold_tasks()
            reclaimed = _reclaim_leases(tasks)
            mine = [t for t in tasks.values()
                    if t.get("status") == "pending" and t.get("to") in ("", None, sid)]
            if mine:
                mine.sort(key=lambda t: (t.get("priority") != "high", t.get("updated_ts", 0)))
                t = mine[0]
                if claim:
                    ts = now()
                    _append_jsonl(_p("tasks.jsonl"), {
                        "ts": ts, "ts_iso": iso(ts), "event": "claim", "task_id": t["task_id"],
                        "status": "running", "by": sid, "by_name": _name_of(sid),
                        "to": sid, "to_name": _name_of(sid),
                    })
                    _append_jsonl(_p("events.jsonl"), {
                        "ts": ts, "ts_iso": iso(ts), "event": "claim",
                        "session_id": sid, "task_id": t["task_id"], "name": _name_of(sid),
                    })
                    t["status"] = "running"
                return {"ok": True, "task": _public_task(t), "reclaimed": reclaimed}
            if now() >= deadline:
                return {"ok": True, "task": None, "reclaimed": reclaimed,
                        "hint": "暂无待领任务。可继续 bus_next_task(wait_seconds=60) 待命，"
                                "或先用 bus_list_peers 确认自己是否被指定为该任务的执行者。"}
        time.sleep(0.25)


def progress(sid: str, task_id: str, note: str = "", percent: int | None = None) -> dict:
    with _lock():
        tasks = _fold_tasks()
        if task_id not in tasks:
            raise BusError(f"任务 {task_id} 不存在")
        ts = now()
        _append_jsonl(_p("tasks.jsonl"), {
            "ts": ts, "ts_iso": iso(ts), "event": "progress", "task_id": task_id,
            "status": tasks[task_id].get("status") or "running",
            "progress_note": note or "", "percent": percent,
            "by": sid, "by_name": _name_of(sid),
        })
    return {"ok": True, "task_id": task_id, "percent": percent, "note": note, "at": iso(ts)}


def report(sid: str, task_id: str, status: str = "done", result: str = "",
           artifacts: str = "") -> dict:
    if status not in ("done", "failed", "cancelled"):
        raise BusError("status 只能是 done / failed / cancelled")
    with _lock():
        tasks = _fold_tasks()
        t = tasks.get(task_id)
        if not t:
            raise BusError(f"任务 {task_id} 不存在")
        ts = now()
        _append_jsonl(_p("tasks.jsonl"), {
            "ts": ts, "ts_iso": iso(ts), "event": "report", "task_id": task_id,
            "status": status, "result": result or "", "artifacts": artifacts or "",
            "by": sid, "by_name": _name_of(sid),
        })
        _append_jsonl(_p("events.jsonl"), {
            "ts": ts, "ts_iso": iso(ts), "event": "report", "session_id": sid,
            "task_id": task_id, "status": status, "name": _name_of(sid),
        })
        owner = t.get("from")
        if owner and owner != sid:
            send(sid, owner,
                 f"【任务回执 {task_id}】{t.get('title')}\n状态：{status}\n"
                 f"{('结果：' + result) if result else ''}"
                 f"{('\n产物：' + artifacts) if artifacts else ''}".strip(),
                 kind="task_report" if status == "done" else "task_failed",
                 correlation_id=task_id)
    return {"ok": True, "task_id": task_id, "status": status, "at": iso(ts),
            "notified": t.get("from_name") or t.get("from") or ""}


def cancel_task(sid: str, task_id: str, reason: str = "") -> dict:
    with _lock():
        tasks = _fold_tasks()
        if task_id not in tasks:
            raise BusError(f"任务 {task_id} 不存在")
        ts = now()
        _append_jsonl(_p("tasks.jsonl"), {
            "ts": ts, "ts_iso": iso(ts), "event": "cancel", "task_id": task_id,
            "status": "cancelled", "result": reason or "被调用方取消",
            "by": sid, "by_name": _name_of(sid),
        })
    return {"ok": True, "task_id": task_id, "status": "cancelled"}


def list_tasks(sid: str = "", status: str = "", mine: bool = False, limit: int = 50) -> dict:
    """任务看板（**纯读操作，不写盘**）。

    设计修正：租约超时只在这里"显示为 pending"（附带 ``lease_expired`` 标记），
    真正的回收写在 ``next_task`` 里发生。这样"读"没有副作用，回收记录也不会被
    一次查看动作悄悄消费掉 —— 这个问题是码道自己的智能体写租约用例时暴露的。
    """
    with _lock():
        _touch(sid, required=False)
        tasks = _fold_tasks()
        ts = now()
        rows = []
        for t in tasks.values():
            expired = t.get("status") in ("claimed", "running") and _lease_expired(t, ts)
            view_status = "pending" if expired else t.get("status")
            if status and view_status != status:
                continue
            if mine and sid not in (t.get("from"), t.get("to"), t.get("by")):
                continue
            row = _public_task(t)
            if expired:
                row["status"] = "pending"
                row["lease_expired"] = True
                row["lease_note"] = "租约已超时（显示为 pending，下一次领活时正式回收）"
            rows.append(row)
    rows.sort(key=lambda r: r.get("updated_ts", 0), reverse=True)
    rows = rows[: max(1, int(limit or 50))]
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"count": len(rows), "summary": summary, "tasks": rows}


def _public_task(t: dict) -> dict:
    return {
        "task_id": t.get("task_id"), "title": t.get("title"),
        "status": t.get("status"), "priority": t.get("priority", "normal"),
        "from_name": t.get("from_name"), "to_name": t.get("to_name") or "（公共池）",
        "by_name": t.get("by_name", ""), "instructions": t.get("instructions", ""),
        "result": t.get("result", ""), "artifacts": t.get("artifacts", ""),
        "progress_note": t.get("progress_note", ""), "percent": t.get("percent"),
        "updated_ts": t.get("updated_ts", 0), "updated_iso": iso(t.get("updated_ts", 0)),
    }


# ---------------------------------------------------------------- 守护/审计视图

def ps(sid: str = "") -> dict:
    ts = now()
    with _lock():
        reg = _load_registry()
        tasks = _fold_tasks()
        msgs = _read_jsonl(_p("messages.jsonl"))
    rows = []
    for k, s in reg["sessions"].items():
        st = _status_of(s, ts)
        rows.append({
            "session_id": k, "name": s.get("name"), "role": s.get("role"),
            "state": st, "pid": s.get("pid"), "project": s.get("project"),
            "last_seen": iso(s.get("last_seen")),
            "age_seconds": round(ts - float(s.get("last_seen", 0)), 1),
            "unread": _unread_count(k),
            "tasks_active": sum(1 for t in tasks.values()
                                if t.get("to") == k and t.get("status") in ("pending", "claimed", "running")),
            "msgs_sent": sum(1 for m in msgs if m.get("from") == k),
        })
    rows.sort(key=lambda r: r["last_seen"] or "", reverse=True)
    return {"bus_version": BUS_VERSION, "home": str(STATE_HOME), "at": iso(ts),
            "sessions": rows,
            "tasks": {s: sum(1 for t in tasks.values() if t.get("status") == s)
                      for s in ("pending", "running", "done", "failed", "cancelled")},
            "messages_total": len(msgs)}


def log(sid: str = "", limit: int = 50, kind: str = "") -> dict:
    with _lock():
        events = _read_jsonl(_p("events.jsonl"))
        msgs = _read_jsonl(_p("messages.jsonl"))
    feed = []
    for e in events:
        if kind and e.get("event") != kind:
            continue
        feed.append({"ts": e.get("ts", 0), "ts_iso": e.get("ts_iso"),
                     "kind": "event", "event": e.get("event"),
                     "name": e.get("name"), "task_id": e.get("task_id", ""),
                     "text": f"{e.get('event')} {e.get('task_id') or ''}".strip()})
    for m in msgs:
        if kind and m.get("kind") != kind:
            continue
        feed.append({"ts": m.get("ts", 0), "ts_iso": m.get("ts_iso"), "kind": "message",
                     "event": m.get("kind"),
                     "name": f"{m.get('from_name')} → {m.get('to_name')}",
                     "task_id": m.get("correlation_id", ""),
                     "text": (m.get("content") or "")[:200]})
    feed.sort(key=lambda x: x["ts"], reverse=True)
    feed = feed[: max(1, int(limit or 50))]
    return {"count": len(feed), "feed": feed}


def stats() -> dict:
    with _lock():
        reg = _load_registry()
        msgs = _read_jsonl(_p("messages.jsonl"))
        tasks = _fold_tasks()
    ts = now()
    online = [s for s in reg["sessions"].values() if _status_of(s, ts) != "offline"]
    return {
        "sessions_total": len(reg["sessions"]), "sessions_online": len(online),
        "messages_total": len(msgs),
        "messages_by_kind": _counter(m.get("kind") for m in msgs),
        "tasks_total": len(tasks),
        "tasks_by_status": _counter(t.get("status") for t in tasks.values()),
        "bus_host_pid": os.getpid(),
    }


def _counter(seq) -> dict:
    out: dict[str, int] = {}
    for x in seq:
        if x:
            out[x] = out.get(x, 0) + 1
    return out


def leave(sid: str, note: str = "") -> dict:
    with _lock():
        reg = _load_registry()
        sess = reg["sessions"].get(sid)
        if sess:
            sess["state"] = "offline"
            sess["last_seen"] = 0            # 立刻视为离线
            sess["notes"] = note or sess.get("notes", "")
            _write_json_atomic(_reg_path(), reg)
        _append_jsonl(_p("events.jsonl"), {
            "ts": now(), "ts_iso": iso(), "event": "leave",
            "session_id": sid, "name": _name_of(sid),
        })
    return {"ok": True, "session_id": sid, "state": "offline"}


def reset(confirm: bool = False) -> dict:
    """清空总线（仅用于演示/测试）。"""
    if not confirm:
        raise BusError("需要 confirm=true 才会清空总线数据")
    with _lock():
        for f in ("registry.json", "cursors.json", "tasks.jsonl", "messages.jsonl", "events.jsonl"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(_p(f))
        for f in _p("inbox").glob("*.jsonl"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(f)
    return {"ok": True, "cleared": True, "home": str(STATE_HOME)}


if __name__ == "__main__":                                  # 手工自检
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        print(json.dumps({"home": str(STATE_HOME), "stats": stats()}, ensure_ascii=False, indent=2))
    else:
        print("agent-bus core. 用 bus_cli.py / bus_mcp.py 调用。")
