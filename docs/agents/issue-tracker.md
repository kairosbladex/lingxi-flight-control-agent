# Issue tracker: Local docs

本仓的 spec / PRD / 工单是 `docs/` 下的 markdown 文件——不是 GitHub/GitLab Issues。
Git 远端是自建 GitLab，但不在那里建 issue。

## Conventions

- 一个工单一个文件：`docs/<TICKET>_<slug>.md`，顶部写 `Ticket:` 与 `Status:` 行。
- 编号：本仓此前没有编号习惯，新建工单从 `FC-<NNN>` 顺序号开始（`FC-001` 起）。
  <!-- 若团队已有统一编号规则，只替换这一行即可，其余约定不受影响。 -->
- 阻塞关系：在工单文件顶部写 `Blocked by: FC-001, FC-002`；所列工单全部完成前，该工单不算就绪。
- 验收：在工单文件内维护 `## 验收` 段，写清可验收结果与所用证据；证据文件按 `docs/evidence/<topic>-<YYYYMMDD>.*` 命名。
- 进展：跨会话继续时在工单文件末尾追加简短进展，不新建汇总大文件。
- Triage 状态：需要时在顶部写 `Status:` 行，取值见 [triage-labels.md](triage-labels.md)。

## When a skill says "publish to the issue tracker"

在 `docs/` 下按上面的命名新建工单文件。**绝不创建 GitHub/GitLab issue。**

## When a skill says "fetch the relevant ticket"

直接读该工单文件。只有编号或状态不明时，才在 `docs/` 下按编号或 slug 搜索定位。

## Wayfinding operations

供 `/wayfinder` 使用。地图与子工单都放在 `docs/`：

- **Map**：`docs/<effort>_map.md`，承载 Notes / 已定决策 / 未决迷雾。
- **Child ticket**：`docs/<effort>-<NN>_<slug>.md`，`NN` 从 `01` 起。用 `Type:` 行记录 `research`/`prototype`/`grilling`/`task`，用 `Status:` 行记录 `claimed`/`resolved`。
- **Blocking**：文件顶部 `Blocked by: NN, NN`；所列项全部 `resolved` 才算解除。
- **Frontier**：扫 `docs/<effort>-*` 中未完成、未阻塞、未认领的文件，编号最小者优先。
- **Claim**：先写 `Status: claimed` 再动手。
- **Resolve**：在 `## Answer` 段写结论，置 `Status: resolved`，并把要点加链接追加到 `map.md` 的已定决策。
