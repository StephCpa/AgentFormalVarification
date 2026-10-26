# 工作流持久执行规格

`DurableWorkflowExecutor` 由 PostgreSQL schema v3 引入，schema v4 增加容量等待。支持 Prepare、Dispatch、Abort、Move、Drop、Alias、If、Send、Reconcile；工作流 v0.5 通过 schema v7 任务服务支持 Close，v0.6 通过 schema v6 注册快照服务支持 Validate，v0.7 通过 schema v8 委派服务支持 Delegate。内存 `WorkflowExecutor` 可作为不含 Context/Task 程序的快速参考执行器，并支持 Delegate。

## 持久记录

|记录|内容与作用|
|---|---|
|workflow_run|租户/主体/run_id、工作流及合约摘要、提交摘要、原始 IR、规范化分支输入、故障配置、引用环境、已分配/已消费操作集合、执行位置 pc、revision 与状态|
|workflow_step|每节点唯一的已提交轨迹；序号与节点唯一；禁止修改/删除|
|workflow_operation|每操作唯一的工作流归属，防止另一 run 再分配同一 Prepared 许可|
|workflow_io|节点级发送/查询意图、操作绑定、工作者、代际、租约、InFlight/Recorded/Uncertain|

定义、输入、故障配置与摘要在创建后不可更新；pc 不回退，revision 必须增长，终止态不能原地恢复为运行态。这里的触发器只约束正常 SQL 写入，不能抵抗数据库管理员删除约束。连接凭据与政策密钥不会进入工作流报告。

输入引用沿用可信注册表检查，批准输入不能别名复用。checkpoint 只保存闭合数据编码的 Request、字符串、许可二元组及不可变集合，没有 pickle、eval 或外部可执行对象。每次执行从保存的 IR 和原政策快照重新加载、检查。纯分支条件只依赖冻结值，因此分支路径可确定性重建；实际选择也记入不可修改的 If 轨迹。合流以显式内部 Join 步骤删除不应继续存在的变量，不删除账本预留。

## 事务边界

每个本地命令在同一租户 guard / SERIALIZABLE 事务中读取 pc、恢复引用环境、运行内核转移，再提交操作、预算、引用消费、归属、节点轨迹和新 pc。提交前进程退出不会留下半个 Prepare；提交后退出会从下一个节点续办。Prepare 的请求键仍由 run_id、工作流摘要和节点 ID 派生，且分配结果必须保持 Prepared、新鲜、合约匹配及未被其他工作流占有。

政策改变后，未执行的旧合约 Prepare 被拒绝并记录 Halted；历史在途操作保留原摘要，仍能核对。改变同一 run_id 的工作流、引用、分支输入或测试故障配置会产生 WORKFLOW_RUN_CONFLICT。

|崩溃位置|恢复行为|
|---|---|
|Prepare/Dispatch 提交前|命令、轨迹、引用消费与账本一起回滚|
|本地命令提交后|读取新 pc，不重复分配/消费|
|发送意图已提交，尚无回执|有效租约期间返回 WaitingIO；过期后标记不确定、保留额度和义务，不盲目重发写请求|
|远端效果已发生，回执丢失|同一操作进入只读核对或持久监督；保持原幂等键|
|原始回执已提交，投影未提交|原始观察恢复程序应用账本，再从 checkpoint 继续|
|账本已结算，工作流步骤未提交|识别已持久终局，补齐步骤，不重新发送|
|旧 I/O 工作者晚返回|有效事实仍可进入独立观察入口；过期 worker 不能推进后续节点|

Send 在事务中复检隔离位、原假设和指定操作的 outbox，提交发送意图后才进行外部 I/O。已经在途、需要核对或等待上界已过的操作不再次写发送，而标明 RequiresReconciliation。Reconcile 只使用当前模拟器的免费只读 query；真实收费查询仍需维护预算。

工作流所属操作始终由其运行控制发送，通用 claim_outbox 不得领取。既有 workflow_operation 归属在每次事务恢复时加载，实际 Send 节点提供宿主发送上下文，统一发送门禁据此核对归属并记录 run_id/node_id。Pending 状态本身不授予通用发送权；Finished、Halted、重启和租约丢失均不转移归属。无需 schema 迁移即可保护旧工作流队列行，详细规则见 [F2 修复规格](review-fixes-profile.md)。

这是保守的恢复协议，不是“物理调用恰好一次”的分布式承诺。已经拿到发送意图的工作者仍可能晚发包，外部幂等与终局协议仍是前提；网络凭据隔离、远端去重有效期和真实服务重启仍需验证。

## 使用和结果

宿主创建 `DurableWorkflowExecutor(postgres_kernel, refund_simulator)`，以 `run(program, subject, run_id, refs, values)` 提交并执行。重启后可直接 `resume(subject, run_id)`，无需重新提供输入。`start`、`advance`、`finish_io` 用于受控宿主分步执行与故障测试；不作为未经认证的 Agent API。

运行状态为 Running、WaitingIO、WaitingCapacity、Finished、Halted。WaitingCapacity 保留原 pc 和未消费批准，容量释放后可以显式续办。Finished 仍只表示有限命令程序完成。Drop 许可后可能留下 Prepared 预留；发送不确定且无终局证据时，程序可以结束而操作保持 Unknown，监督义务继续存在。数据库执行器没有把“程序结束”提升为业务 completion。

并发执行者共用持久 pc 和节点 I/O 意图；其中一个可观察到 WaitingIO，之后读取已完成结果。终止结果保留，重复 run 返回原结果。Halted 的失败不能通过改输入或回退 pc 原地消除；重新规划与人工处置接口尚未实现。

## 验证及剩余工作

集成测试覆盖本地事务提交前/后子进程退出、准入后退出、发送意图后退出、远端效果由独立父进程模拟对象保留后的原始回执/投影/检查点故障、并发执行、错配回执、跨主体读取拒绝、分支恢复、合流、Move/Alias/Abort、跨运行许可复用和 v1/v2 升级。未将模拟器内存恢复测试宣称为真实远端服务恢复测试。

完整验证输出 `durable-workflow-demo` 的 IR 与三个轨迹：丢回执后核对、Drop 保留预留、发送结果不明后换执行器核对。其中报告实际 send 调用数、远端成本、账本和 I/O 意图，而不只报告最终状态。

容量门禁及 WaitingCapacity 见 [容量规格](capacity-profile.md)，Close 的事务、状态与历史快照见 [任务关闭规格](task-close-profile.md)，Validate 的证据与检查点事务见 [候选验证规格](candidate-validation-profile.md)，Delegate 的能力与检查点事务见 [委派规格](delegation-profile.md)。尚缺 propose、子任务自动启动与监督交接、通用验证器、生产认证与出口隔离、收费维护额度、容量公平调度、可信时间区间、真实持久远端及旧备份恢复、编译与 SQL 精化证明。当前全租户加载策略也尚未完成生产负载测试。
