# 独立预览服务的鉴权边界（内网测试版）

2026-10-15。解决的真问题：独立预览服务原先直接读客户端自带的 `X-Agent-Actor`，
**谁都能冒充别的操作者**；而且它监听 `0.0.0.0`、CORS 是 `*`，位姿与预览卡还按
`(actor, 项目, 机库, 无人机, 任务)` 分键 —— 也就是说冒充别人就能读到别人的上下文。

## 拓扑

```
浏览器 ──(X-Operator-Token)──► 网关 0.0.0.0:8794 ──(X-Agent-Actor + X-Agent-Gateway)──► 预览服务 127.0.0.1:8793
                                    │                                                        │
                             操作者清单 + 来源白名单 + 审计日志                        共享凭证校验（hmac.compare_digest）
```

四道约束缺一不可：

1. **预览服务只监听回环地址**：外部无法直连，唯一的入径是网关。
2. **网关丢弃客户端身份头**（`X-Agent-Actor` / `x-agent-gateway` / `X-Operator-Token` /
   `Authorization`，含大小写变体），再按操作者清单注入 `actor_id`。
3. **服务端校验共享凭证**（`--gateway-token-env`）：凭证不对一律 `401 UNAUTHENTICATED`，
   客户端自带的 `X-Agent-Actor` 不再被当作身份。`/healthz` 例外，供守护进程探活。
4. **CORS 收窄**为显式来源白名单（网关侧），不再是 `*`。

身份仍是「持有 token 的人」而不是 SSO：token → `actor_id` 由操作者清单决定，
一人一 token，轮换 = 改清单 + 重启网关。生产要接统一身份，应在网关这一层接，
而不是把 token 下发给浏览器。

## 操作者清单

```json
{
  "tokens": {
    "<64位随机token>": { "actor_id": "wxl-operator", "label": "王晓林" }
  }
}
```

清单与共享凭证都在服务器上、权限 `600`，**不进仓库**。清单缺失、为空、`actor_id`
含控制字符或超长时，网关**拒绝启动**（fail closed，不以宽松模式运行）。

## 启动

```bash
# 预览服务：回环 + 共享凭证
export LINGXI_GATEWAY_TOKEN=$(openssl rand -hex 32)
PYTHONPATH=src python3 tools/standalone_preview_server.py \
  --host 127.0.0.1 --port 8793 --gateway-token-env LINGXI_GATEWAY_TOKEN

# 网关
python3 tools/agent_gateway.py \
  --host 0.0.0.0 --port 8794 --upstream http://127.0.0.1:8793 \
  --operators-file /home/user/flight-agent-preview/operators.json \
  --allowed-origin http://192.168.110.17 \
  --gateway-token-env LINGXI_GATEWAY_TOKEN \
  --log-file /home/user/flight-agent-preview/logs/gateway.log
```

前端把 token 放 `X-Operator-Token`（或 `Authorization: Bearer`）请求头；首次可用
`?operator_token=<token>` 打开，页面存入 localStorage 后从地址栏抹掉。缺 token 时
面板锁定、不发请求。

## 验证矩阵（部署时实测）

| # | 动作 | 期望 |
| --- | --- | --- |
| 1 | 内网直连 `238:8793` | 连接被拒（只监听回环） |
| 2 | 过网关但**不带 token** | `401 UNAUTHENTICATED` |
| 3 | 过网关带**错误 token** | `401 UNAUTHENTICATED` |
| 4 | 过网关带**正确 token** | `200`，且服务记录的操作者是该 token 对应的 `actor_id` |
| 5 | 用 A 的操作者写位姿，用 B 的操作者读 | B 得到 `404 POSE_NOT_FOUND`（身份真的隔离） |
| 6 | 客户端伪造 `X-Agent-Actor` 过网关 | 被丢弃，服务看到的是网关注入的身份 |
| 7 | 非白名单来源的预检 / 请求 | `403 ORIGIN_NOT_ALLOWED` |
| 8 | 网关审计日志 | 每条请求一行：时间、操作者、方法、路径、状态、耗时 |
