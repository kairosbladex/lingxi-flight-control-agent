# 自然语言飞控交互交付记录

2026-10-16，Asia/Taipei。当前交付范围是本地可运行的软件候选；真实设备控制、共享部署和正式测试驾驶舱视觉验收没有执行。

## 任务卡与基线

目标：用户以自然语言提出无人机动作，由大模型理解、必要时补参，展示完整动作卡，逐项确认后交给受控执行器；前端使用指定 GitLab `wangxiaolin/lingxi-flight-agent-ui`，物理执行复用 `ktd_backend`。

验收：真实服务/内存模拟器的澄清、预览、确认、取消闭环；动作契约及多输入测试；Java 请求编译、权限/租约/状态门禁、持久幂等与未知回执；前端隔离浏览器点击验证。非目标：任务规划 Agent、直接 MQTT、未核验机型支持、共享环境发布和真机飞行精度结论。

| 项目 | 开工基线 | 工作区 |
| --- | --- | --- |
| 飞控 Agent | `refactor/agent-project-naming` / `c93ab9a67555eb46b448204a1615ab70ff7f0839` | 干净；origin=`git@192.168.110.2:wangxiaolin/lingxi-flight-control-agent.git` |
| 指定前端 | `refactor/agent-project-naming` / `e02eb37`；正式 `main=5e1b319` 为祖先 | 干净；origin=`git@192.168.110.2:wangxiaolin/lingxi-flight-agent-ui.git` |
| Java，只读 | `feature/cockpit-control-proxy` / `dd11a47f91d7bde83a67b09537432dd2453a8a6d` | 干净，未修改 |
| 旧驾驶舱参考，只读 | `feature/cockpit-control-preview` / `b009a80bf1d2755ab41ebd1cddd6ee18922e5125` | 原有合并冲突和用户改动完整保留 |

开发分支两仓库均为 `feature/natural-language-cockpit`。运行身份是本机开发 Agent；Python 3.13.2，Node/Vite 使用现有前端依赖。没有读取或导出平台登录凭据。配置来自命令行和既有 Ollama 默认配置；平台 token 仅允许由服务端注入，不在模型/浏览器/日志中流转。

后端变更集中在 `cockpit.py`、`ktd_platform.py`，以及 `ollama_decider.py` 向后兼容的可选生成上限；新增启动器、三个bench脚本、三个测试模块和输入冻结集。前端变更集中在 `CockpitPanel.vue`、`flightControl.ts`、入口开关、本地代理和测试；旧面板移至 `LegacyPreview.vue` 保留。相关README、接口说明和 `docs/evidence/` 记录交付证据。无数据库迁移、Java业务文件或实际运行配置修改。

初始核对预算40次调用/30分钟；实现与复核阶段上限120次开发工具调用/90分钟。模型冻结评测上限100样本、160调用、900秒、2,000,000估算tokens；本地模型提供方不返回费用。真实飞行和共享发布属于独立人工授权节点。

## 调用链与兼容

```mermaid
flowchart LR
  UI[指定 Vue 前端] --> API[CockpitService 服务端身份与会话隔离]
  API --> LLM[现有 OllamaDecider + BoundedAgentLoop]
  LLM --> POLICY[工具 schema / 参数证据 / 确认卡]
  POLICY --> HUMAN[用户逐项确认]
  HUMAN --> EX[现有 RuntimeExecutor 接口]
  EX --> SIM[内存模拟器]
  EX --> KTD[默认关闭的 KtdPlatformExecutor]
  KTD --> JAVA[既有 Java 控制接口]
```

没有新增另一套 AgentLoop/ToolRegistry。新 HTTP facade 复用原有动作定义、有限循环、CheckpointStore 和执行器；旧 `/sessions/*`、`/api/flight-preview/*`、模拟器及公共导入保持兼容。新交互不使用旧 SessionRunner 自动确认的演示路径。

模型只有提议权：写工具的 handler 永远禁止调用；模型返回动作后由执行器预览。确认直接执行保存的卡，不再次让模型改变参数。复合指令每次只展示一个动作，用户说“继续”才规划下一步，每一步再确认。取消、新意图、会话重置、设备版本或控制权变化使旧卡失效；模拟执行不能升级为真实设备效果。

`ACTION_SPECS` 仍是59项动作契约的来源；交互提议把单次位移进一步限制为0.01–20米。参数必须有用户数值/单位依据，支持中文数词、小数、厘米换算、半米与常见速度表达。明确否定、条件、延迟和能力询问采取保守澄清；该策略不是覆盖所有自然语言的形式证明，模型候选仍需逐项确认。

每次模型循环最多3步，每会话最多30次输入。本地启动入口每次模型HTTP限20秒、每次生成限2048 tokens、最多一次尝试，服务总耗时检查65秒。每tab独立会话；最多64个会话，闲置30分钟回收。模型迟到结果在取消/重置后被丢弃。检查点保存失败不能吞掉已经获得的执行回执。急停等精确安全短语复用原有安全快路径，即使模型不可用也可生成待确认卡；仍要求用户确认。

## 新 HTTP 契约

浏览器只访问 `/api/flight-control`，每页发送随机 `X-Agent-Session`；它只是隔离标识，不是授权。操作者和唯一设备绑定由服务端建立，消息不能携带 actor、任意URL、原始DTO或替换设备。平台模式必须配置可信网关，验证 `X-Agent-Gateway` 和服务端注入的 actor；默认启动器只监听 `127.0.0.1` 并只允许模拟器。

| 接口 | 请求 | 结果 |
| --- | --- | --- |
| GET `/context`、`/state` | 会话头 | 服务端上下文、状态、59项能力及当前可用标记 |
| POST `/agent/messages` | `text,request_id,context_version` | `clarification / preview_required / final / rejected / error` |
| POST `/agent/confirm`、`/agent/cancel` | `confirmation_id,confirmation_binding,command_id,idempotency_key,context_version` | 原动作的结构化回执 |
| POST `/agent/reset` | `context_version` | 旧卡失效；前端成功后轮换会话标识 |

新卡 `expires_at` 是Unix秒。真实Java任务ID可为空，不用任务批次补造。响应包含 `execution_mode,dispatched,device_effect,trace_id`；`device_effect` 始终为 `not_verified`。同请求标识不能更换输入，已被替换的预览不能通过重试复活。平台使用持久 SQLite journal，在发送前记录未知结果；超时、崩溃或重复确认不能自动重发同一指令。

## 本地运行

```bash
# 后端：真实大模型理解，动作只在内存模拟器执行
PYTHONPATH=src python3 tools/cockpit_server.py --port 8794

# 指定前端仓库
cd ../lingxi-flight-agent-ui
npm run dev -- --host 127.0.0.1
# http://127.0.0.1:5173/flight-agent-ui/
```

模拟器以经纬度113/23、航向90度、高度50米的虚构在空状态启动。先输入“申请控制权”并确认模拟接管，再输入“向前飞”→补充“十米”→核对卡片→确认。也可以直接询问状态。旧页面通过 `?mode=preview` 保留。不要把本地模拟器截图或模型回答当作真机飞行证据。

离线浏览器验收使用 `tools/bench/cockpit_ui_server.py` 的明确脚本模型夹具；它只用于检查UI/HTTP/模拟器流程，不能证明模型理解能力。前端沿用现有蓝色聊天面板和两张素材；无法下载旧 `onegismap_a` 最新远端源码，因此完整测试驾驶舱一致性未验收。

## 验证命令与证据

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_cockpit.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_ktd_platform.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_intent_scenarios.py'
bash tools/ci/offline_tests.sh
PYTHONPATH=src python3 tools/bench/ktd_http_smoke.py

# 模型理解候选：无确认、无平台工具、无飞行
PYTHONPATH=src python3 tools/bench/cockpit_intents.py --live --max-samples 100 \
  --max-model-calls 160 --max-seconds 900 --max-tokens 2000000 --timeout 30 \
  --output docs/evidence/cockpit-intents-20261016.json
```

TDD覆盖了正常路径、补参、非法/超限参数、否定/延迟、取消、超时与预算、过期/篡改/跨会话确认、并发和恢复、操作者/资源/权限/控制权变化，以及平台状态与发送结果分层。模型冻结集为100条合成输入、17类、覆盖57/59个动作；未覆盖上传航线和投放器。没有独立人工或飞行真值，业务准确率 `not_evaluable`；只报告合成契约匹配及逐类分母，错误/超时/缺失保留。

首轮评测工具在发送前因缺失 `num_predict` 报错，实际模型调用0，保留在 `evidence/cockpit-intents-harness-error-20261016.json`。修复后新增贯穿真实解码器/传输边界/循环的回归测试，再冻结运行；这份失败记录不属于模型能力评测。

| 验证层 | 结果 | 证据边界 |
| --- | --- | --- |
| 后端全量离线 | 398项通过 | 含整套禁止socket连接的守护测试 |
| 前端契约/状态测试 | 46项通过 | 原预览兼容 + 新交互、安全边界 |
| 前端构建 | Vue/TS检查、Vite构建通过 | 当前指定仓库，非共享部署 |
| 浏览器，HTTP夹具 | 12类场景通过 | `evidence/cockpit-ui-fixture.json`，0越界请求/浏览器异常 |
| 浏览器，真实服务+模拟器+脚本脑 | 5类场景通过 | `evidence/cockpit-ui-service.json`，10次API/2次确认，0设备请求 |
| 浏览器，真实qwen3.5:9b+服务+模拟器 | 第2次尝试5类场景通过 | `evidence/cockpit-ui-live-model.json`，10次API/2次模拟确认，0越界/设备请求；首次失败另保留说明 |
| Java HTTP传输回环 | 通过 | 2个本机假服务请求，0平台请求；验证固定请求编译和不跟随重定向 |
| 模型候选首轮 | 分母100：76匹配、7不匹配、8超时、9缺失 | `evidence/cockpit-intents-20261016.json`，900秒预算，94次模型调用，无确认/业务动作 |
| 模型候选补跑 | 仅原9条缺失：4匹配、5超时 | `evidence/cockpit-intents-supplement-20261016.json`，独立时间窗；报告仍保留100分母，其余91条为未选样本 |

首轮逐类分母保存在JSON中。7项不匹配包含4项“否定输入得到澄清”这种保守分类差异；另有“执行上传航线”被理解为恢复航线，以及“十来米”被当作10米、条件返航被处理为只读状态查询。报告的 `unsafe_action_proposals=2` 中只有1项是移动提议，另1项为只读查询，不能称作2次危险执行。真实服务对这两项均有独立重放测试并拦截；航线语义混淆仍是模型候选已知限制，不自动纠正为成功。

首轮p50约6.70秒、p95约30.01秒；超时调用的token使用量未知，已知部分为320902输入/11590输出，费用未提供。首轮输入、标签、模型、prompt、工具schema与源文件哈希均记录且运行期间未变化。后续修复只涉及交互策略/会话和回执边界，未改变该轮所用提议registry、模型prompt和解码器；这些改动另由离线回归验证。补跑仅针对首轮9条缺失输入，使用同一输入/标签/prompt/schema/模型设置，另存报告，不覆盖原来的7项不匹配或8项超时。

两次窗口的输入、标签、prompt、schema和模型options哈希/内容均已比较一致；100个不同输入最终都尝试过，总计80匹配、7不匹配、13超时。匹配中有1条由现有输入护栏直接拒绝，不能称作模型作答；合计103次模型调用，确认、平台请求和实际设备动作均为0。超时是已知待改进项，不能从分母剔除，也不能把80/100称作真实业务准确率。当前只冻结模型tag，未取得权重revision证明。

真实模型浏览器首次尝试在“申请控制权”未获得动作卡，页面保持未持有控制权且没有确认/执行；随后同一服务直接提议请求与独立循环诊断均返回 `control_acquire` 待确认，`done_reason=stop`。首次错误未记录细分模型原因，不能断言是超时，也不能把后续成功覆盖为始终稳定。固定脚本模型的UI测试不包含此类模型可用性问题。

第2次真实模型浏览器闭环通过：申请控制权并确认→“向前飞”得到距离澄清→“10米”产生前进10米卡→确认模拟移动→拍照卡取消→新会话。根Agent目视复核了 `evidence/cockpit-ui-live-model-confirmation.png`，并另从服务读回位置/状态；这仍是虚构资源上的软件闭环，不是Java或真机执行验收。

前端已提交并推送 `6dbb5182ba2b00891e01a9dd47a4cd62e66f42d3`，远端分支SHA一致；后端提交以本分支最终Git记录为准。两个GitLab origin保持不变，未创建合并请求，未同步未映射的个人仓库。

## 尚未闭合的真实环境条件

详见 [Java契约核对](ktd-platform-contract.md)：现有距离实现与能力声明不一致；起飞高度基准、偏航角度和当前点降落不能强行映射。现有正式HTTP还不能提供同时经过资源鉴权的完整位姿、新鲜度、控制权租约和逐动作许可；调试OSD接口不作为替代。没有可关联的设备完成回执，HTTP200仅能标记 `submitted`。

需要Java负责人提供并确认权威只读snapshot契约、逐机型能力和可关联设备回执；再在隔离测试资源上做获授权的集成、人工界面与设备验收。本轮已尝试只读核对Java `origin/prod`，GitLab明确返回“not allowed to download code”，无法取得按项目规则开发Java补充接口所需的最新正式基线。此轮不修改Java、不部署、不调用业务/设备接口、不合并dev-wxl/test、不创建合并请求。

回滚：不启用平台执行器；前端设置 `VITE_FLIGHT_AGENT_MODE=preview` 返回旧预览入口，或回退本功能提交。新服务停机后未执行卡失效。已经提交的真实指令不可能靠回退代码撤销，持久journal必须保留；本轮没有真实提交。
