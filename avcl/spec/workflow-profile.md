# 工作流 v0.7 实现规格

本实现落实详细设计第 5.2、5.3、6、7 章中的仿射资源、效果检查与有限控制结构。它是受限结构化 IR 和参考执行器，不是已证明的编译器、任意 Python 沙箱或完整工作流服务。

## 输入与边界

当前格式 `avcl/workflow/0.7` 绑定 id、contract_digest、refs、values、effects、body、source_map。refs 的可输入种类为 Candidate、Capability、Evidence、Approval、Task、Context，并带工具作用域；Permit、Operation、TaskReport 和 ChildCapability 不能从工作流参数伪造进入。values 使用纯谓词 IR 类型，只用于无副作用分支条件。加载器仍接受 v0.4/v0.5/v0.6，并保留各自原格式、规范化内容和域分离摘要，不自动升级持久程序。

Candidate 在宿主中对应 Request，其他输入引用只是服务端注册项的名字。静态声明不会把名字变成授权：prepare 仍加载注册记录、校验主体/作用域/批准/预算；未注册或不匹配引用会拒绝。当前宿主将 subject 视为已认证身份，进程认证/隔离仍未实现。

工作流是候选计划，不是政策发布权。加载时与宿主活动合约摘要匹配，并重新运行类型/效果/仿射分析；合流注解被篡改会拒绝。运行到 prepare 时，在同一内核锁中再核对工作流合约版本，防止预检后政策切换造成错误绑定。原操作的发送/核对保留已准入快照语义。

## 命令语义

|命令|资源转换|效果与实际行为|
|---|---|---|
|Delegate|读取父 Capability，输出 ChildCapability；不消费父能力或签发批准|Read/ResourceConsume；调用在线委派服务，产物与节点检查点同事务，未创建子进程|
|Validate|读取 Candidate 和 Context，产生 Evidence；不消费批准或签发能力|Read/ResourceConsume；固定退款验证器只读注册快照，证据与工作流检查点同事务|
|Prepare|消费 Approval，产生 Permit；Candidate/Capability/Evidence 可复用|ResourceConsume；调用内核原子预留|
|Dispatch|消费 Permit，产生 Operation|ResourceConsume；只进行本地派发准入|
|Send|使用已有 Operation，不产生新许可|采用签名合约注册工具的全部效果；以原操作键调用模拟远端|
|Reconcile|使用已有 Operation|Read/Disclose/ResourceConsume；查询原操作，缺记录不能释放|
|Close|使用宿主登记的 Task，产生可复用 TaskReport|Read/ResourceConsume；关闭请求与检查点同事务，不取消操作或释放预算；需持久任务后端|
|Abort|消费 Permit|ResourceConsume；仅取消未派发准备并释放其预留|
|Move|消费旧变量名并建立新仿射变量名|无外部效果；许可身份不变|
|Alias|只允许可复用引用|无外部效果；Approval/Permit 复制拒绝|
|Drop|移除变量|无外部效果；不撤销批准，不删除账本承诺|
|If|求值纯条件，选择一个分支|静态效果仍覆盖两个分支|

输入同一实际批准引用绑定到两个 Approval 变量会在任何动作前拒绝。不同准备语句如果经业务去重返回同一个许可，也不能形成两个可消费运行句柄。已经准入或属于旧合约的返回操作不会被当成新许可使用。

Operation 可以查询或按同一远端幂等键重发，不能当作 Permit 再准入。当前没有 Propose、循环或任意 ToolCall 的工作流执行后端；未知命令明确拒绝，不能降级执行任意宿主代码。Validate/Close 由 PostgreSQL 后端执行；内存执行器在任何动作前拒绝含 Context/Task 输入的程序，分别返回 WORKFLOW_REQUIRES_DURABLE_VALIDATION_BACKEND 或 WORKFLOW_REQUIRES_DURABLE_TASK_BACKEND。Delegate 在内存和持久执行器均调用已登记的委派服务，详见 [委派规格](delegation-profile.md)。

Validate 的形状为 `{"tag":"Validate","candidate":"r","context":"ctx","validator":"order-service","out":"e"}`。validator 是合约中登记的字面 ID，不是客户端上传的代码或函数。当前只实现 order-service / RefundEligible / sim/0.1；静态检查不能把结构校验器登记冒充退款资格验证器。Context 是宿主订单快照引用，不是普通字典。完整语义、过期与崩溃边界见 [候选验证规格](candidate-validation-profile.md)。

## 仿射环境和分支

可复用引用与仿射引用在同一显式环境中带不同种类。消费移除当前名字；历史定义集合防止重绑定已用名字。分支从输入环境各自检查，合流仅保留两个输出环境都存在、类型一致的变量。许可在任一分支被消费或丢弃后，就不能在合流处再使用。

两个分支各自建立同名同类型的新许可可以合流，运行时只创建被选择分支的那一笔准备。合流处消失的变量属于仿射弱化，执行器只删除局部变量，不触碰账本事实；在内存与持久后端都没有自动释放动作。

所有分支先检查，即使条件是常量 false，也不能隐藏重复消费、非法字段或未声明效果。效果为分支并集，必须包含在 workflow.effects 内。此规则有意保守，不使用求解器分析分支不可达性来放宽硬约束。

单工作流最多 1000 命令、控制深度 32、128 个输入、64 KiB 编码。分支条件继承纯 IR 的总定义性与资源上限。静态分析报告用 `compilation_status=Accepted`，编译器元理论仍为 `Pending`；不冒用 ProvedUnder。

## 执行、异常与事实保留

WorkflowExecutor 是受信宿主对象，只使用固定退款模拟适配器。Dispatch 与 Send 是独立步骤：程序在 Dispatch 后结束时没有远端效果，但账本仍保留全部承诺。Send 在内核锁内检查隔离状态和原合约假设，写 SendAttemptAdmitted 后释放锁，再调用远端；并不在网络期间持有事务锁。

已经获准的发送可能在并发撤销/隔离之后完成，不能声称隔离线性化后远端绝无效果。后续尚未准入的发送会被阻止，核对路径可继续读取已有事实。进程中直接调用模拟器仍可绕过此路径，所以 R16 部署全中介尚未完成。

超时、网络错误、丢回执、查询缺记录均保持预留，首次不确定结果将 Dispatched 转为 Unknown 并建立现有截止记录。未知适配器异常会停止程序并保留 Unknown，不输出原始异常文本。返回回执必须同时匹配当前 operation_id 和请求摘要，才能交给观察入口；错配或不可用回执不会替其他操作结算，会保留当前承诺并隔离新危险动作。

模拟查询费用为零。效果标记 ResourceConsume 不等于付费查询已取得额度；真实收费核对的独立维护预算和授权仍是待实现项。

程序状态为 Finished 或 Halted，独立于 Operation 的执行/结算状态。Finished 只代表命令结束，不能推导业务成功、义务履行、没有 Unknown 或预算已释放。报告始终返回创建/遇到的操作 ID，按当前内核事实读取执行、结算和剩余承诺，不导出 Permit 作为新授权。

内存执行器内的 subject+run_id 重复提交同一输入返回已有结果；更改输入返回冲突，Halted 也不自动重跑。该后端的缓存和程序计数不持久化。新增的 [PostgreSQL 工作流执行器](durable-workflow-profile.md) 将定义、引用环境、操作归属、程序计数和节点 I/O 意图持久保存，并单独验证提交与恢复边界；它不扩大本文的语言子集或编译证明结论。

已有运行使用记录的历史合约重新加载并核对完整提交；新运行使用当前合约。政策升级不阻断同键同参的历史读取，也不允许同键异参通过。工作流 Prepare 绑定操作发送归属，统一发送门禁仅接受实际 Send 节点的宿主上下文，见 [审阅修复规格](review-fixes-profile.md)。

## 验证范围

测试覆盖重复消费、批准/许可复制、Candidate 假冒证据、Operation 假冒许可、移动、分支合流、死分支效果、显式取消与丢弃区别、同操作幂等重试、回执丢失、传输异常、适配器超界、错配回执、政策切换竞态和 IR 注解篡改。

独立的可用位 oracle 对 18 种二分支骨架判断资源是否还能消费；10 种可接受骨架各执行两个分支，共 20 条实际账本路径。这是有界交叉检查，不是所有程序的类型健全性证明。

演示同时保留两个结果：proceed=true 时远端成功后丢回执，随后核对，actual=spent=70、pending=0；proceed=false 时丢弃许可变量，actual=spent=0、pending=70。两个程序分支都 Finished，而业务和承诺事实不同。
