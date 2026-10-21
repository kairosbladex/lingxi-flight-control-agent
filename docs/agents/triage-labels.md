# Triage Labels

skills 用五个固定的 triage 角色说话。本仓不用 issue tracker 标签，而是把角色写进工单文件顶部的 `Status:` 行。

| skills 中的角色 | 本仓 `Status:` 取值 | 含义 |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | 待维护者评估 |
| `needs-info` | `needs-info` | 等待提出方补充信息 |
| `ready-for-agent` | `ready-for-agent` | 规格完整，可交给 AFK Agent |
| `ready-for-human` | `ready-for-human` | 需要人来实现 |
| `wontfix` | `wontfix` | 决定不做 |

`/wayfinder` 另用两个状态：`claimed`、`resolved`（见 [issue-tracker.md](issue-tracker.md)）。

当某个 skill 提到某个角色（例如"打上 AFK-ready 标签"），就把对应的取值写到工单文件顶部的 `Status:` 行。
右侧取值列可按团队实际词汇修改，左侧角色名不要改。
