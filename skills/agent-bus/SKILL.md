---
name: agent-bus
description: 码道会话总线（agent-bus）：让同一台机器上的多个码道会话/智能体互相通信与协作。当任务需要多个并行会话分工（一个会话拆任务、其他会话分别执行再汇总）、当一个会话需要把子任务转交另一个会话、当你要询问或通知另一个正在运行的会话、当需要在多个会话之间传递结论或上下文时使用本技能。也适用于：多会话各自开发不同模块需要对接、一个会话当"调度者"其他当"执行者"、长任务需要在会话间同步进度。本技能配合 agent-bus MCP 服务器使用，核心工具为 bus_register / bus_list_peers / bus_dispatch / bus_next_task / bus_report / bus_inbox。
---

# 码道会话总线（agent-bus）

码道默认能同时开多个会话，但它们**彼此不知道对方存在**，也无法传递信息 —— 本技能把多个会话
变成一支能分工的"团队"。

## 触发场景（什么时候用）

| 场景 | 用哪些工具 |
|---|---|
| 一个任务能拆成 2~5 块并行做 | leader：`bus_dispatch` 多次；worker：`bus_next_task` |
| 想知道现在还有哪些会话在跑、各自在干嘛 | `bus_list_peers` / `bus_ps` |
| 要把一个结论/文件路径/约定告诉另一个会话 | `bus_send` 或 `bus_broadcast` |
| 在等另一个会话的产出，不知道它做完没 | `bus_inbox(wait_seconds=60)` 或 `bus_list_tasks` |
| 长任务，怕对方以为你卡死 | `bus_progress` |
| 要给人演示多会话协作的实时画面 | `bus_dashboard` |

## 角色约定（很重要，先定角色再动手）

* **leader（调度者）**：拆任务、派活、收口、汇总。**不要自己闷头把所有活都干完**。
* **worker（执行者）**：领活、上报、回执。**干完必须 `bus_report`**，否则 leader 会一直等。
* **reviewer（审查者）**：只读、给意见，用 `bus_send` 回给作者。

## 标准协作四步（照抄即可）

**第 1 步：每个会话先报名（只在开始时做一次）**

```
bus_register(name="planner", role="leader", capabilities="需求拆解/验收/汇总")
bus_register(name="worker-frontend", role="worker", capabilities="HTML/CSS 实现")
```

名字要**唯一且见名知义**。同一个 `name` + 同一个项目 = 同一个身份，重复调用只是刷新心跳。
报名后先看一眼有谁在线：`bus_list_peers()`。

**第 2 步：leader 拆任务并派发**

```
bus_dispatch(
  title="实现 index.html 首页骨架",
  instructions="产出单文件 HTML，含标题区与任务列表区；验收标准：浏览器打开无报错。只改 index.html。",
  to="worker-frontend",
  priority="high",
  artifacts="index.html"
)
```

* `to` 留空 → 进**公共池**，谁空闲谁领。
* `instructions` 要写清：做什么、验收标准、涉及文件、**边界（不许改什么）**。
* 一次不要派超过 3~5 个子任务；码道同机并行会话数量有限。

**第 3 步：worker 领活 → 干活 → 回执**

```
bus_next_task(wait_seconds=60)     # 待命领活；有任务立刻返回，没有就等最多 60 秒
bus_progress(task_id="t_xxxx", note="骨架已完成，正在补样式", percent=60)
bus_report(task_id="t_xxxx", status="done",
           result="index.html 已生成，含标题区与任务列表区，浏览器打开无报错",
           artifacts="index.html")
```

**第 4 步：leader 收口**

```
bus_list_tasks()          # 谁没回执，一眼看穿
bus_inbox(wait_seconds=60)  # 等还没回来的回执
bus_dashboard()           # 生成协作看板，用于汇报/答辩
```

leader 汇总时把每个子任务的结果与产物路径列清楚，再给最终结论。

## 工具速查

| 工具 | 一句话 |
|---|---|
| `bus_register` | 报名/续用身份（先做这个） |
| `bus_whoami` | 我是谁、几条未读、几个同伴 |
| `bus_list_peers` | 谁在线、能干什么 |
| `bus_send` / `bus_broadcast` | 定向/广播发消息 |
| `bus_inbox(wait_seconds=N)` | 收信；**N>0 时阻塞等待**，这是待命领活的正确姿势 |
| `bus_reply` | 回复某条消息 |
| `bus_dispatch` | 派任务（带优先级、租约、期望产物） |
| `bus_next_task` | 领任务（优先自己的，其次公共池） |
| `bus_progress` | 上报进度，防租约超时 |
| `bus_report` | 回执 done/failed/cancelled（**必做**） |
| `bus_list_tasks` / `bus_cancel_task` | 任务看板 / 撤单 |
| `bus_ps` / `bus_log` | 守护视图 / 事件流水 |
| `bus_dashboard` | 生成协作看板 HTML |
| `bus_leave` | 下线 |

## 注意事项（踩过坑的人都懂）

1. **MCP 是"智能体主动调用"**：总线不会主动弹消息给你。所以 worker 必须**定期或阻塞式**去
   `bus_inbox(wait_seconds=60)` / `bus_next_task(wait_seconds=60)` 取活；leader 也必须主动
   `bus_inbox` 才能收到回执。**不调用 = 收不到**。
2. **等待要有上限**：`wait_seconds` 建议 30~120，别写 600 以上，长等会让会话看起来"卡住"。
3. **不要空转**：`bus_inbox` 返回空之后，别立刻无限循环重试；应该去干手上的活，或者用带
   `wait_seconds` 的单次调用待命。
4. **任务必须闭环**：领了活就要最终 `bus_report`（哪怕是 `failed` 也要报，并写清卡在哪）。
   默认租约 300 秒，超时任务会自动退回公共池 —— 长任务请用 `bus_progress` 续命。
5. **文件归属要写清**：多个 worker 改同一个文件必然冲突。派活时在 `instructions` 里指定
   "只允许改哪些文件"，并优先让不同 worker 负责不同文件。
6. **产物用绝对路径**：`artifacts` 里写完整路径，别写"就是刚才那个文件"。
7. **不要把大段代码塞进消息**：消息传结论、决策、文件路径；细节让对方自己读文件。
8. **收工前 `bus_leave`**，避免其他会话对着一个已经结束的会话发消息。

## 示例：一句话触发两人的协作

用户对**会话 A**说：

> 用总线把这个首页拆成"页面骨架"和"样式美化"两件事，派给另外两个会话做，做完汇总给我。

会话 A（自动扮演 leader）：`bus_register` → `bus_list_peers` → `bus_dispatch` ×2 →
`bus_inbox(wait_seconds=60)` 等回执 → 汇总。

**会话 B / C**（用户对它们说"你是 worker，去总线上领活"）：`bus_register` →
`bus_next_task(wait_seconds=60)` → 干活 → `bus_report`。
