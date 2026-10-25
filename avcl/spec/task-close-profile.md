# 显式任务关闭与当前完成状态

依据详细设计第 10.3 节，schema v7 增加任务范围、显式关闭请求和可持续更新的任务状态。工作流 `Finished` 仍仅表示指令程序结束；不会自动变成业务 `Completed`。

## 先登记范围，再开始执行

宿主通过 `TaskAuthority.register(subject, task_key, expected_business_keys)` 登记任务，得到不透明 task_ref。当前退款配置要求 1—32 个预期业务键，主体、逻辑任务键、预期集合和完成合约摘要不可修改。登记必须先于该主体/逻辑任务的任何操作。系统不会从已经成功的操作反推任务范围，也不会在旧库升级时替旧操作生成“已完成任务”。

登记时的活动合约必须包含 1—16 条退款 completion 条款。默认安全合约没有 completion，因此不能直接登记为可关闭任务。宿主需经过原有政策发布流程添加业务完成条件；候选代码或客户端的 success 布尔值不能替代此步骤。

任务内新准备和派发必须属于登记的业务键集合；封闭任务拒绝新准入。准确的已有请求重试和已准入操作的幂等查询仍保留。未注册的旧调用路径继续原有账本语义，但不能取得任务完成保证。完整语言的任务作用域强制检查仍待补齐。

后续安全政策更新可为任务中的新操作授予当前有效权限；已签发许可仍按原规则失效。任务的业务成功标准保留登记时的合约摘要，不能通过后来改成较弱的 completion 条款改变旧任务的目标。登记同一任务键、不同范围或不同当前合约会拒绝，不隐式重新定义任务。

## 完成条件来自持久事实

关闭按照主体和 request.task 从整个租户账本收集相关操作，与预先登记的业务集合比对。客户端不能提交操作子集。

完成谓词支持以下服务端生成的字段：confirmed、rejected、partial、aborted、settled、amount、max_fee、charged_principal、charged_total、currency。实际费用从 Reservation 的 charged 字段读取，执行和结算状态来自 Operation。未知字段或不受支持的字段类型在任务登记时拒绝；没有客户端上下文覆盖入口。

|任务状态|当前退款配置的判断|
|---|---|
|Running|尚未收到显式 close，即使操作已结算也不宣称任务已完成|
|Pending|缺少预期动作，效果/费用/义务未终局，或 completion 仍无法成立|
|Completed|预期集合完整，全部操作 Confirmed 且 Final，预留为零、相关义务无未履行或超期记录，全部 completion 条款通过|
|Rejected|范围内全部操作最终为 RejectedNoEffect 或 AbortedBeforeDispatch，且无未决事项|
|Escalated|尚未终局的全部操作都已有注册接收方的持久接收确认；不能只凭已发送通知|
|Failed|存在费用争议、义务超期、终局部分/混合失败，或终局业务违反完成条款|

先有明确争议或超期时可报告 Failed，同时仍保留未决操作和预算。已确认交接也不清除 Unknown 或释放预留。关闭过程不取消操作、不执行补偿、不调用远端、不删除义务、不修改费用。真正的准入前取消和远端事实结算仍由原有协议负责。

completion 是结果判断，不是新的授权条件；不会被用于证明派发时的硬安全要求。任务级“所有退款成功”门禁同时要求 Confirmed，不能用一个恒真 completion 把拒绝或部分效果称为成功。

## API 与幂等

|接口|认证与输入|返回|
|---|---|---|
|POST /v1/tasks/{task_ref}/close|close scope；正文仅 client_request_id|当前任务状态、冻结合约摘要、操作修订和义务计数|
|GET /v1/tasks/{task_ref}|get scope；无正文|最新任务评估|

两条路由均复检服务端凭据，跨主体、跨租户和不存在的任务返回相同 TASK_NOT_FOUND。客户端不能传 status、success、operation_ids 或 expected_business_keys。凭据撤销后，重复 close 也不返回缓存。

TaskCloseRequest 以租户、主体、task_ref 和请求键防重，保存首次评估的不可修改回执。同键重试返回当前投影而非永久复用旧成功状态，不新增关闭回执或事件。第一次关闭若为 Pending，关闭意图持久保留；后续事实满足条件时，任务投影自动更新，不要求再发一条关闭请求。

## 迟到事实与封闭范围

task_run 保存当前报告与修订号；task_close_request 保存历史调用回执。每次持久写入在结算和义务同步后重评任务，状态或依赖修订变化写入 TaskAssessmentChanged。GET 也在事务中检查截止和当前依赖，可记录新发现的超期。

进入 Completed、Rejected、Escalated 或 Failed 后，sealed 标志不可撤回，阻止新的危险准入。其后仍必须接纳并记录现实事实：迟到费用可使 Completed 变 Failed，已接收转交的操作最终成功后可从 Escalated 变 Completed。旧关闭回执与超期记录保持原样；封闭不意味着账本冻结。

报告是当前可核验的状态，不是不可撤销的永恒保证。完成合约使用声明的依赖与当前健康信息；依赖不足时保持 Unknown/Pending，不能伪造成功。当前字段投影只列每操作的 completion 状态与未通过条款数，具体条款可由冻结合约摘要定位；尚未提供完整逐条诊断查询接口。

## 持久化与验证

任务登记、关闭、准入和现实观察共用租户 guard 与 SERIALIZABLE 事务。task_run 的定义与 sealed/close_requested 单调标志由触发器保护；历史关闭回执禁止改写或删除。schema v6 升级保留原操作、凭据、验证证据和工作流，不推测旧任务范围。

集成测试包含完整成功、缺少预期动作、独立完成条款、未决手续费、Unknown、终局拒绝/部分效果、确认交接、超期后成功、关闭后迟到争议、工作流 Finished 但任务 Pending、范围与身份注入、并发关闭、API 重启、凭据撤销和提交前后子进程退出。

首轮 19 项定向测试中 2 项错误源于测试夹具：常量谓词给了不受支持的空字段模式，工作流故障节点误写为 n0.2 而不是 w0.2。已修正夹具，未放宽编译器或工作流故障计划校验；该次运行不能作为全通过证据。最终数量与结果以完整 verification.json 为准。

## 剩余范围

这实现了 PostgreSQL、认证 API 和工作流 v0.5 的显式任务关闭，不构成完整任务时序逻辑的机械证明。内存任务后端、通用多工具成功条件、计划修订/补偿任务关系、关闭报告的完整诊断 API、生产身份/隔离和大规模增量重评仍需实现。当前实现逐租户扫描任务与操作；32 个业务键和 16 个完成条款是此配置的显式边界，不是设计中的吞吐或 10000 在途恢复验收。

## 工作流 Close 指令

v0.5 使用 `{"tag":"Close","task":"task_ref_variable","out":"report_variable"}`。Task 是可复用的服务端引用，在工作流启动事务复检主体归属；TaskReport 是数据快照，不能当作 Capability、Evidence、Approval、Permit 或 Task 使用。两个分支都生成同类型报告时才能在合流后使用；未声明 Read/ResourceConsume 效果的 Close 即使位于未执行分支也会被拒绝。

Close 不等待任务成功。它可以返回 Pending、Escalated、Rejected、Failed 或 Completed，然后程序继续运行；例如 Close(Pending) → Reconcile → Close(Completed)。整体程序 Finished 仍不代表成功，调用方必须检查 TaskReport 或任务查询结果。

TaskCloseRequest、工作流步骤、输出报告与 pc 在一个事务提交。关闭请求键由 run_id、工作流摘要和 node_id 确定；提交前退出全部回滚，提交后重启不会重复创建关闭回执。报告以闭合数据编码保存，工作流步骤记录 task_ref、task_revision 和关闭请求键。

步骤中的任务状态是执行当时的快照；后来核对或迟到费用可能改变当前任务状态，但不会修改历史步骤和已保存的报告。查询当前状态仍使用任务 API。v0.4 程序及旧检查点保持原摘要，不能通过修改版本标记夹带新 Task 类型或 Close 指令。

`examples/close_workflow_demo.py OUTPUT_DIRECTORY` 生成已确认、未决、核对后完成三条数据库轨迹与工作流 IR；`verify.cmd --postgres` 将这些产物一并封存。内存执行器当前没有任务服务，不会先执行退款再遇到 Close 才报不支持，而是在任何动作前拒绝所需后端能力。
