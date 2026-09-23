# 码道会话总线 · agent-bus

> 给**华为云码道（CodeArts）代码智能体**补上官方文档里没有的能力：**会话与智能体之间的通信与协作**。
>
> 码道能同时开多个会话并行干活，但它们彼此既"看不见"也"喊不应"。agent-bus 在本地架起一条总线，
> 让一个会话可以当调度者拆解任务、其他会话领活执行、结果自动回传汇总 —— 直接对标 Claude Code
> 泄露源码中**尚未发布**的 `UDS Inbox`（跨会话 IPC）与 `Daemon Mode`（会话守护）。

---

## 它解决什么问题

| 现状 | 装上 agent-bus 之后 |
|---|---|
| 4 个会话各干各的，改了同一份文件才发现冲突 | 派活时写明"只准改哪个文件"，冲突在派发阶段就被避免 |
| 会话 A 的结论要靠你人工复制到会话 B | `bus_send` 直接传，`correlation_id` 自动串起整条线索 |
| 不知道另一个会话做完了没，只能反复切窗口看 | `bus_inbox(wait_seconds=60)` 阻塞待命，回执自动送达 |
| "多会话并行"只是各跑各的，拼不出一个完整交付 | leader 拆→worker 干→`bus_report` 回执→leader 汇总，闭环 |

## 架构（一张图看懂）

```
              码道 IDE / 码道 CLI（同一台机器、同一个用户）
      ┌────────────────────┐        ┌────────────────────┐
      │ 会话 A（leader）    │        │ 会话 B / C（worker）│
      │ 智能体              │        │ 智能体              │
      └─────────┬──────────┘        └─────────┬──────────┘
                │ stdio / streamableHttp       │
                ▼                              ▼
      ┌──────────────────────────────────────────────────┐
      │  bus_mcp.py（每会话一个 MCP 子进程，无共享内存）    │
      │  bus_http.py（给 Space / 远程用的 HTTP 端点）      │
      └──────────────────────┬───────────────────────────┘
                             ▼  跨进程文件锁 + 原子写
      ┌──────────────────────────────────────────────────┐
      │  bus_core.py —— 共享状态（~/.codeartsdoer/agent-bus）│
      │   registry.json  会话注册表（心跳/角色/状态）        │
      │   inbox/*.jsonl  每会话一个收件箱（append-only）     │
      │   tasks.jsonl    任务事件流（状态机 + 租约）         │
      │   events.jsonl   审计流水 ──► dashboard.html 看板    │
      └──────────────────────────────────────────────────┘
```

* **17 个工具**：报名 / 同伴发现 / 收发消息 / 派活 / 领活 / 进度 / 回执 / 任务看板 / 守护视图 /
  流水 / 看板 / 下线。
* **纯标准库**：不装任何第三方包，拷过去就能跑（码道"无需配置"的调性）。
* **两种接法**：`stdio`（IDE/CLI 插件）+ `StreamableHTTP/SSE`（Space、网页版、远程）。

## 三步用起来

```bash
# 1) 自检（不依赖码道，先确认机器上能跑）
python tests/test_e2e.py          # 35 项检查，含真实 MCP 协议握手

# 2) 一键跑完整协作剧本 + 生成看板
python src/bus_cli.py demo
python src/bus_cli.py dashboard --open

# 3) 接进码道：把 config/mcp_settings.stdio.json 的 mcpServers 合并进
#    码道 设置 → MCP工具 → 配置MCP，再把 skills/agent-bus 拷进项目 .codeartsdoer/skills/
```

详见 [`docs/03-安装部署.md`](docs/03-安装部署.md)（含故障排查）。

## 目录结构

```
codearts-agent-bus/
├── src/
│   ├── bus_core.py        # 共享状态层：注册表/消息/任务状态机/文件锁/原子写
│   ├── bus_mcp.py         # MCP stdio 服务端（手写 JSON-RPC 2.0，零依赖）
│   ├── bus_http.py        # StreamableHTTP / SSE 传输（给 Space 与远程）
│   ├── bus_cli.py         # 人用命令行 + 演示剧本
│   └── bus_dashboard.py   # 自包含单页看板（拓扑 + 任务 + 消息流）
├── skills/agent-bus/      # 码道技能：告诉智能体什么时候用、怎么协作
├── config/                # 三种 mcp_settings.json 示例（stdio / http / 混合）
├── tests/
│   ├── test_e2e.py            # 组件级：核心链路 + 跨进程并发 + MCP 协议握手（35 项）
│   └── test_two_sessions.py   # 集成级：两个独立 MCP 进程 = 码道的两个会话（19 项）
├── 自检.bat / 一键演示.bat / 装技能到个人级.bat
├── demo/dashboard-sample.html   # 看板样张（可直接双击打开）
└── docs/                  # 缺口分析 / 设计 / 安装 / 演示 / 作品发布材料
```

## 实测结果

```
[1] 核心协作链路       26 项 PASS（注册幂等、派活、长轮询、租约回收、回执、看板…）
[2] 跨进程并发         5 项 PASS（6 进程同时写：无丢失、无重复）
[3] MCP stdio 协议     10 项 PASS（initialize / tools/list 17个 / tools/call / -32601 / 干净退出）
    → test_e2e.py 全部通过 ✅

[4] 双会话全链路       19 项 PASS（两个独立 MCP 进程：发现同伴→派活→收信→领活→
                        进度→回执→看板→广播→下线）
    → test_two_sessions.py 全部通过 ✅
```

两条命令复现：`python tests/test_e2e.py` 与 `python tests/test_two_sessions.py`
（Windows 上也可以直接双击 `自检.bat`）

> 看板样张：`demo/dashboard-sample.html`（离线可开，无任何外链）

## 边界与已知限制

* 同机同用户才能互通（定位就是"本机会话总线"，不替代跨机器方案）。
* 会话身份绑定在 MCP 子进程内存里，**进程重启后需要重新 `bus_register`**（技能里已写清）。
* 消息为文本；大文件走 `artifacts` 传路径，不塞消息体。
* 不做鉴权之外的加密：状态目录是本机用户私有目录；HTTP 模式默认只监听 `127.0.0.1` 且强制 token。


---

## 与码道智能体的协作实证（2026-09-23）

本项目不只"用码道"，还**让码道自己的智能体参与到开发里** —— 这是产品能力最硬的证据。

| 时间 | 事件 | 证据 |
|---|---|---|
| 20:28:35 | 码道 IDE 自动拉起本项目的 MCP 子进程（PID 5600） | 进程命令行 `python bus_mcp.py` |
| 20:28:35 | **码道的智能体注册上总线**：`codearts-worker`（role=worker） | `registry.json` / `events.jsonl` |
| 20:28:47 | 外部调度者（CLI 扮演 planner）派发高优先级任务 `t_0f9f4c3b` | `tasks.jsonl` dispatch |
| 20:29:44 | **码道智能体调用 `bus_next_task` 领走任务** | `tasks.jsonl` claim |
| 20:30–20:37 | 三次 `bus_progress`："正在精读 src/bus_core.py" → "已设计 5 个 TestCase + tmpdir 隔离" → "测试文件已写（248 行）" | `tasks.jsonl` progress |
| 20:37 | 产出 `tests/test_bus_core_unit.py`（5 个 TestCase / 12 个用例）并自行运行 | 文件 248 行 |

**它写出来的测试真的抓到了我的问题**（我独立复跑验证，不是它自述）：

1. `并发写不应抛异常` → `PermissionError(13)`：Windows 上 `os.replace`（原子写）撞上并发读会失败。
   → 已修复：`_write_json_atomic` / `_read_json` 增加退避重试。
2. `test_lease_timeout_reclaims_to_pending` → 拿不到回收记录：因为 `list_tasks()` 这个**只读视图**
   偷偷调用了 `_reclaim_leases` 产生写副作用，把回收事件"消费"掉了。
   → 已修复：新增纯判断 `_lease_expired()`，`list_tasks` 改为**纯读**，回收只发生在 `next_task`。

修复后重跑：**它的 13 个用例全部 OK**（`Ran 13 tests … OK`），我自己的测试套件也从 35 项扩到 **37 项全通过**。

> 状态说明：截至本文档写入时，码道智能体已产出文件但尚未提交 `bus_report` 回执（其租约已超时被显示为 pending），
> 因此这个子任务在总线上标记为"待收尾"；这不影响上述链路证据的有效性。
