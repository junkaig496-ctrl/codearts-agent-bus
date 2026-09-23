"""
agent-bus 协作看板：把总线快照渲染成一个自包含单页 HTML（无外链、无 CDN、离线可看）
===============================================================================
生成物是「快照式」的：每次调用都会用最新状态覆盖写同一份文件，浏览器刷新即更新。
这么做是为了让它在码道 IDE 内嵌浏览器、Space、答辩投屏里都能直接用 —— 不依赖任何服务。
"""

from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_core as core  # noqa: E402

STATE_COLOR = {
    "online": ("#22c55e", "在线"),
    "idle": ("#38bdf8", "空闲待命"),
    "working": ("#f59e0b", "工作中"),
    "offline": ("#94a3b8", "离线"),
    "unknown": ("#94a3b8", "未知"),
}

TASK_COLOR = {
    "pending": "#f59e0b",
    "running": "#38bdf8",
    "done": "#22c55e",
    "failed": "#ef4444",
    "cancelled": "#94a3b8",
}


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _layout(n: int, cx: float = 470, cy: float = 250, rx: float = 350, ry: float = 175):
    """把 n 个会话均匀摆在一个椭圆上。"""
    import math
    pts = []
    for i in range(n):
        a = -math.pi / 2 + 2 * math.pi * i / max(1, n)
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def build(open_after: bool = False, out: Path | None = None) -> Path:
    snap = core.ps()
    feed = core.log(limit=40)["feed"]
    # 注意：这里必须用关键字参数，否则 30 会被当成 mine 过滤器（踩过一次）
    tasks = core.list_tasks(status="", limit=30)["tasks"]
    stats = core.stats()

    out = out or (core.home() / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)

    sessions = snap["sessions"]
    pts = _layout(len(sessions))

    # ---- SVG 拓扑：总线在中间，会话围一圈
    nodes = []
    for (s, (x, y)) in zip(sessions, pts):
        color, label = STATE_COLOR.get(s["state"], STATE_COLOR["unknown"])
        nodes.append(f"""
        <g class="node">
          <line x1="470" y1="250" x2="{x:.1f}" y2="{y:.1f}" stroke="#334155" stroke-width="1.5"
                stroke-dasharray="{'4 4' if s['state'] == 'offline' else '0'}" opacity="0.7"/>
          <circle cx="{x:.1f}" cy="{y:.1f}" r="46" fill="#0f172a" stroke="{color}" stroke-width="3"/>
          <text x="{x:.1f}" y="{y - 6:.1f}" text-anchor="middle" fill="#e2e8f0"
                font-size="13" font-weight="700">{_esc((s['name'] or '')[:12])}</text>
          <text x="{x:.1f}" y="{y + 12:.1f}" text-anchor="middle" fill="{color}"
                font-size="11">{_esc(label)}</text>
          <text x="{x:.1f}" y="{y + 28:.1f}" text-anchor="middle" fill="#64748b" font-size="10"
                >{_esc(s['role'])} · 未读{s['unread']}</text>
        </g>""")

    svg = f"""
    <svg viewBox="0 0 940 500" role="img" aria-label="会话拓扑">
      <circle cx="470" cy="250" r="74" fill="#1e293b" stroke="#38bdf8" stroke-width="3"/>
      <text x="470" y="242" text-anchor="middle" fill="#e2e8f0" font-size="16" font-weight="700">agent-bus</text>
      <text x="470" y="264" text-anchor="middle" fill="#94a3b8" font-size="11">会话总线 v{_esc(snap['bus_version'])}</text>
      <text x="470" y="282" text-anchor="middle" fill="#64748b" font-size="10">{_esc(snap['at'])}</text>
      {''.join(nodes) if nodes else '<text x="470" y="180" text-anchor="middle" fill="#64748b" font-size="13">还没有会话注册 —— 在码道里调用 bus_register 试试</text>'}
    </svg>"""

    task_rows = "".join(f"""
      <tr>
        <td><code>{_esc(t['task_id'])}</code></td>
        <td><span class="pill" style="background:{TASK_COLOR.get(t['status'], '#94a3b8')}">{_esc(t['status'])}</span></td>
        <td>{_esc(t['title'])}</td>
        <td>{_esc(t['from_name'])} → {_esc(t['to_name'])}</td>
        <td class="muted">{_esc((t.get('result') or t.get('progress_note') or '')[:60])}</td>
        <td class="muted">{_esc(t['updated_iso'])}</td>
      </tr>""" for t in tasks) or '<tr><td colspan="6" class="muted">暂无任务</td></tr>'

    feed_rows = "".join(f"""
      <tr>
        <td class="muted">{_esc(f['ts_iso'])}</td>
        <td><span class="tag">{_esc(f['kind'])}</span> {_esc(f['event'])}</td>
        <td>{_esc(f['name'])}</td>
        <td class="muted">{_esc(f['text'][:90])}</td>
      </tr>""" for f in feed) or '<tr><td colspan="4" class="muted">暂无事件</td></tr>'

    badge = "".join(
        f'<span class="kpi"><b>{v}</b>{k}</span>' for k, v in (
            ("在线会话", stats["sessions_online"]),
            ("消息总数", stats["messages_total"]),
            ("任务总数", stats["tasks_total"]),
            ("未完成", stats["tasks_by_status"].get("pending", 0) + stats["tasks_by_status"].get("running", 0)),
        ))

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta http-equiv="refresh" content="5"/>
<title>agent-bus · 码道会话协作看板</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#020617; color:#e2e8f0;
         font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif; }}
  header {{ padding:20px 28px 12px; border-bottom:1px solid #1e293b; }}
  h1 {{ margin:0 0 6px; font-size:20px; }}
  h1 span {{ color:#38bdf8; }}
  .sub {{ color:#94a3b8; font-size:12px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:14px; }}
  .kpi {{ background:#0f172a; border:1px solid #1e293b; border-radius:10px;
          padding:8px 14px; font-size:12px; color:#94a3b8; }}
  .kpi b {{ color:#e2e8f0; font-size:18px; margin-right:6px; }}
  main {{ padding:18px 28px 40px; }}
  section {{ background:#0b1220; border:1px solid #1e293b; border-radius:14px;
             padding:14px 18px; margin-bottom:18px; }}
  h2 {{ font-size:14px; margin:0 0 12px; color:#cbd5e1; letter-spacing:.5px; }}
  svg {{ width:100%; height:auto; max-height:460px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
  th, td {{ text-align:left; padding:7px 9px; border-bottom:1px solid #16213a; }}
  th {{ color:#64748b; font-weight:600; font-size:11.5px; }}
  code {{ background:#111c33; padding:1px 6px; border-radius:5px; font-size:11.5px; }}
  .pill {{ color:#020617; border-radius:999px; padding:2px 9px; font-size:11px; font-weight:700; }}
  .tag {{ background:#1e293b; border-radius:5px; padding:1px 6px; font-size:11px; }}
  .muted {{ color:#64748b; }}
</style></head>
<body>
<header>
  <h1>码道会话总线 · <span>agent-bus</span> 协作看板</h1>
  <div class="sub">本页每 5 秒自动刷新 · 快照时间 {_esc(snap['at'])} ·
    总线目录 <code>{_esc(snap['home'])}</code></div>
  <div class="kpis">{badge}</div>
</header>
<main>
  <section><h2>会话拓扑（谁在线、在干什么）</h2>{svg}</section>
  <section><h2>任务看板</h2>
    <table><thead><tr><th>任务号</th><th>状态</th><th>标题</th><th>派发 → 执行</th><th>结果/进度</th><th>更新</th></tr></thead>
    <tbody>{task_rows}</tbody></table>
  </section>
  <section><h2>消息与事件流</h2>
    <table><thead><tr><th>时间</th><th>类型</th><th>来源</th><th>内容</th></tr></thead>
    <tbody>{feed_rows}</tbody></table>
  </section>
</main>
</body></html>"""

    out.write_text(doc, encoding="utf-8")
    if open_after:
        import webbrowser
        webbrowser.open(out.as_uri())
    return out


if __name__ == "__main__":
    p = build()
    print(json.dumps({"dashboard": str(p)}, ensure_ascii=False))
