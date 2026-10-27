# V1.4 阶段二后端测绘中断契约记录（2026-10-27）

## 已完成

- `ktd_backend` 提供 `POST /lingxi/agent/mapping/interruption`，请求固定为
  `lingxi-mapping-interruption/v1`，要求项目、站点、任务、批次、确认标志和幂等键。
- 外部拼图停止通过 `MappingStopAdapter` 注入；默认实现返回
  `MAPPING_INTERRUPTION_UNAVAILABLE`，没有同步停止回执时不修改任务状态。
- 真实停止回执确认后，Java 事务按固定六步更新 `rebuildType`、`breakInfo`、断点、恢复锁和拼图外部引用；回执写入唯一幂等表，重复请求复用原回执。
- 拼图重启入口检查任务/拼图恢复锁，防止已作废测绘任务重新恢复。

## 实际验证

- 后端提交：`2ce49f4d22 feat: 增加测绘中断原子事务契约`。
- `mvn -pl biz-inspection -am -DskipTests compile`：BUILD SUCCESS。
- `mvn -pl biz-inspection -am -Dtest=MappingInterruptionContractTest -Dsurefire.failIfNoSpecifiedTests=false test`：3 项通过。
- SQL migration：`doc/sql/2026/20261027-lingxi-mapping-interruption.sql`。

## 未验证 / 不宣称

- 当前默认拼图适配器仍不可用；真实拼图服务同步停止 API、数据库 migration 执行和跨实例幂等尚未验证。
- 未连接共享 UAT/生产、真实 MQTT 或设备。

## 回滚

- 回滚后端提交 `2ce49f4d22` 及对应 migration；已执行 migration 的数据库需按 DBA 方案回滚或保留新增列/表，禁止直接删除未知数据。
