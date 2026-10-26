# AVCL 可执行参考原型

本目录将《AVCL 形式化验证语言与执行系统详细设计 v0.1》的退款闭环落为可运行代码。当前实现退款参考语义、纯谓词 IR、合约与政策更新、有限仿射工作流，以及 PostgreSQL 18.6 持久事务适配器。内存与数据库后端共用状态转移内核；工作流提供内存执行与持久检查点执行两种入口。原始研究文件与详细设计 DOCX 保持不变。

**阶段：M1 实现中，尚未达到 M1 发布门槛。** 这是可验证的语义原型，不是已部署的生产安全网关。数据库事务、持久工作流/监督、容量背压及本机认证 API 已落地；生产身份、密钥隔离、出口控制、通用收费核对、备份恢复限制与完整 IR 仍有缺口，见 [实施状态](spec/implementation-status.md)。

[3e5c63f 审阅修复](spec/review-fixes-profile.md) 覆盖 F1—F4：冗余旧回执保留 Final；工作流发送权不随共享 outbox 暴露；累计费用域覆盖合法本金与手续费之和；历史运行按历史合约核对。新增组合模型、乱序/并发/崩溃回归，验证结果以对应源码摘要的最新报告为准。

[可选 SMT 后端](spec/smt-profile.md) 从同一纯谓词 IR 和生成指令产生输入域非空、一致性与允许动作存在性查询，固定 Z3 4.15.4 并保留模型回放和查询摘要。它提供显式依赖下的局部 ProvedUnder，不调用运行门禁，不替代系统状态转移与部署验证。

[合约联合 SMT](spec/contract-smt-profile.md) 进一步检查条款共享输入、prepare/dispatch 共同见证及假设有效期。联合不可满足时输出带来源和必要性见证的冲突集；它不会删除条款、替换健康事实或发布政策。入口为 `scripts/verify_smt.py --contract INPUT_CONTRACT NEW_OUTPUT_DIRECTORY`，并由 `verify.cmd --smt` 一并验证。

## 运行

Windows 进入本目录执行：

```powershell
.\verify.cmd
```

启动器优先使用当前账户的 Codex Python 运行时，否则使用 PATH 上的 Python。需要 Python 3.12+ 和 Lean 4.33.0；基础参考内核无第三方依赖。Lean 工具链名称固定于 `formal/lean/lean-toolchain`，不会由验证脚本自动更新。若缺少工具，验证记为失败，不跳过后报成功。默认验证明确不包含 PostgreSQL。

数据库验证使用独立 `.venv` 和本目录私有测试实例；已配置环境执行：

```powershell
.\.venv\Scripts\python.exe scripts\local_postgres.py start
.\verify.cmd --postgres
.\.venv\Scripts\python.exe scripts\local_postgres.py stop
```

从空环境安装、数据库范围及故障点见 [持久层规格](spec/postgres-profile.md)。`--postgres` 缺少数据库或驱动时失败；不会以跳过测试充当数据库验证通过。

可选 SMT 验证：在 `.venv` 中按 `requirements-smt.txt` 安装锁定依赖，执行 `.\verify.cmd --smt`；可与 `--postgres` 合用。缺少 Z3 或版本不匹配时本项验证失败。默认验证不包含 SMT；单独文件入口为 `scripts/verify_smt.py INPUT_IR NEW_OUTPUT_DIRECTORY`，见 [SMT 规格](spec/smt-profile.md)。

[M1 门槛审计](spec/m1-acceptance-audit.md) 区分已验证内容与剩余缺口。`.\verify.cmd --m1` 要求私有 PostgreSQL 实例运行，执行 24 项命名检查并记录双进程锁等待及预算拒绝等现场证据；这是定向检查，不等于 M1 发布或全套数据库验证。[核心需求诊断](spec/requirement-diagnostics.md)与[运行求值预算](spec/evaluation-budget-profile.md)已实现；全系统持久拒绝日志及剩余需求覆盖仍需完善。

也可分别运行：

```powershell
python -m unittest discover -v
python -m models.budget_model
python examples/refund_demo.py
Set-Location formal/lean
lean Core.lean
```

每次完整验证新建 `evidence/<UTC时间>/`，保存测试日志、有限模型 JSON、Lean 公理依赖、环境与源码 SHA-256 清单、两个设计文件的基线校验，以及 IR 候选包、政策激活报告和 `workflow-demo/` 下的工作流 IR、分析报告、分支执行报告。失败记录同样保留。`verification.json` 的 `passed` 只表示该报告声明的验证范围通过，不会把候选包内部的 `Unknown` 改成已证明。

[P7 委派组合核心](spec/formal-delegation-profile.md) 以独立 Lean 模型证明权限衰减传递、祖先撤销门禁、共同根与子额度的承诺保持，并构造非空准入/预留/结算实例。`lean-delegation` 阶段逐项记录公理依赖；证明门禁拒绝占位、自定义公理、缺失或重复声明。Python/SQL 实现精化与真实适配器验证仍未完成。

[P4 现实成本桥接](spec/formal-cost-bridge-profile.md) 显式区分真实费用与本地账本，证明在完整费用上界、单调观察和权威终局前提下，任意有限正常执行保持 `实际成本 ≤ spent + pending ≤ limit`。新增有限模型检查 41 状态、135 转移，四种错误协议均产生反例；独立费用源测试覆盖部分结算后的迟到手续费。`lean-cost-bridge` 和 `cost-bridge-model` 留存各自证据，不表示真实适配器已被证明。

[发送窗口与有限保留](spec/send-window-profile.md) 为三个宿主发送入口统一检查冻结截止、逻辑时间余量和至多三次尝试，修复 outbox 过期后仍可被领取的问题。有限保留模拟服务会删除去重记录，并以远端接受截止拒绝迟到报文；记录缺失仍保留 Unknown 预算。独立模型为 90 状态、204 转移，三个变异给出反例。默认模拟服务仍采用永久去重，真实时间与服务契约尚待验证。

[发送鉴权信封](spec/send-envelope-profile.md) 在发送准入事务中绑定操作、完整请求、租户、接收者、用途、截止与尝试序号，使用独立域派生的 HMAC 密钥。可选鉴权模拟服务拒绝原始对象、篡改及跨作用域重放；数据库重开保持同一接收密钥，领取提交后退出仍按核对恢复。验证另生成 `send-envelope-demo/` 的费用与账本轨迹；生产身份与密钥隔离仍待完成。

[回执鉴权与恢复](spec/receipt-envelope-profile.md) 使用独立回执密钥核验操作绑定、累计费用及终局标记，支持发送结果、工作流核对和持久监督器。鉴权要求单调持久化；原始回执与预期操作上下文一起落盘，崩溃恢复仍拒绝跨操作重放。历史真实回执可在发送截止后结算，伪造回执不释放预算。鉴权仅证明消息绑定，远端事实真实性与生产密钥体系仍需验证。

## 可观察的退款案例

预算为 100，第一笔退款为 70，远端实际成功但回执丢失。本地转为 `Unknown`，保持 `spent=0, pending=70`。另一笔 60 的退款被拒绝。以原 `operation_id` 重发并核对后，远端累计只有一次效果，账本变为 `spent=70, pending=0`。

本地准入与远端执行是两个步骤。撤销阻止旧 epoch 的新准入；撤销前已经准入的操作仍可能在远端完成。查询无记录、超时、许可过期都不构成释放已派发预算的依据。远端取消只有返回会阻止同键迟到请求的终态墓碑，才支持无效果结算。

## 目录

|位置|内容|
|---|---|
|`avcl/canonical.py`|受限 JSON 规范化、金额字符串、域分离摘要|
|`avcl/types.py`|请求、引用、账本、操作、观察与审计事件|
|`avcl/ir_types.py`、`avcl/ir.py`|Bool/Int/String/Id/Enum/Record/Option/Set、总定义检查、版本化序列化、摘要与节点定位|
|`avcl/checks.py`|同一 IR 生成的短路栈式门禁；加载时复核全部指令|
|`avcl/verification.py`、`avcl/package.py`|有限域一致性/非空性报告与未签名单谓词候选包|
|`avcl/contract.py`、`avcl/policy.py`|条款分类、工具/验证器依赖、假设、宿主审阅与 HMAC 签名发布封套|
|`avcl/workflow.py`、`avcl/workflow_runtime.py`|有限工作流类型/仿射/效果检查、分支合流、受控退款执行与故障报告|
|`avcl/workflow_postgres.py`|工作流定义/输入冻结、账本与检查点原子提交、I/O 意图及重启续办|
|`avcl/runtime.py`|内存事务、授权/预算/派发/核对参考内核|
|`avcl/postgres.py`、`avcl/storage_codec.py`、`migrations/`|数据库事务、数据编码、关系账本、原始回执、outbox 与租约|
|`avcl/supervision.py`|持久义务扫描、领取代际、只读核对批次、接收确认和超期历史|
|`avcl/capacity.py`|活动操作、未来核对义务和持久工作流的容量门禁及监控投影|
|`avcl/gateway.py`、`avcl/http_gateway.py`、`scripts/runtime_api.py`|宿主签发凭据、冻结 checked_ref、本机 HTTP API 与事务内撤销复检|
|`avcl/validation.py`|公开退款候选验证、可信快照引用、证据依赖复检及验证事务防重|
|`avcl/tasks.py`|冻结任务范围、显式关闭、结果/结算/义务联合评估和历史关闭回执|
|`integration/`|独立数据库连接并发与子进程强制退出故障测试|
|`avcl/simulator.py`|独立远端订单状态、幂等退款及拒绝墓碑|
|`models/budget_model.py`|独立有限状态模型及两种故意错误的协议变体|
|`formal/lean/Core.lean`|抽象 P1/P2/P3 转移核心与证明|
|`tests/`|协议反例、输入拒绝、并发测试、模型边回放|
|`spec/`|需求覆盖、可信边界、证明范围和下一步|
|`scripts/verify.py`|可复现验证与证据归档|

IR v0.2 的编码、类型、Option 守卫、输入域和产物关系见 [IR 实现规格](spec/ir-profile.md)。退款内核执行生成门禁，并将 IR 摘要绑定到许可和准入事件。有限退款示例在其完整 120 输入域内通过解释器/门禁一致性和允许动作存在性检查；实际退款输入域过大，其离线报告保留 `Unknown`，逐次运行检查另行报告。

合约 v0.3 的发布检查、冻结条款、时点与回滚规则见 [合约与政策更新规格](spec/contract-policy.md)。签名通过只是发布检查的一部分：每个硬条款仍须获准的运行检查、需求映射和共同允许见证，依赖与假设须当前有效。政策更新与派发在同一锁下排序，失败更新不改变任何账户、epoch 或活动政策。

[工作流 v0.7](spec/workflow-profile.md) 区分 Delegate、Validate、Prepare、Dispatch、Send、Reconcile 和 Close。许可与一次性批准不能复制，分支消费过的许可不能在合流后重新出现；声明效果覆盖所有分支。执行完成状态 `Finished` 仅表示命令程序结束：丢弃许可后仍可能保留预留，丢回执后仍可能是 Unknown。实际效果和账本状态在执行报告中单独列出。

[持久监督器](spec/supervision-profile.md) 按原截止领取核对义务。工作者重启不延长期限，已超期记录不会因晚结算而消失。转交必须有宿主验证的接收确认，且继续保留 Unknown 预算。`--postgres` 还输出三条监督轨迹的 `supervision-demo/supervision-report.json`。

[持久工作流](spec/durable-workflow-profile.md) 将引用消费和执行位置与账本一起提交。重启可直接按主体和 run_id 续办；发送结果不明时转核对，已落盘的回执不会导致重新发送。`--postgres` 输出 `durable-workflow-demo/` 下的 IR、检查点和实际发送次数。

[容量背压](spec/capacity-profile.md) 在 Prepare 前为未来核对义务保留槽位。满额会拒绝新操作，并使已有工作流停在 WaitingCapacity；核对和结算继续可用，Unknown 转交不释放容量。`kernel.capacity()` 提供活动使用量和余量；完整验证同时运行独立容量模型及两个错误门禁的反例检查。

[运行时 API](spec/gateway-profile.md) 用服务端随机凭据绑定主体与租户，HTTP 客户端只提交已注册的 checked_ref 和授权引用。prepare/dispatch/get/reconcile/cancel 均经事务复检；管理和适配器入口不挂载。`--postgres` 包含实际 HTTP 服务子进程及重启测试，默认不留下常驻服务。

[候选验证 API](spec/candidate-validation-profile.md) 接受退款候选和宿主订单快照引用，签发限定资格证据与 checked_ref。通过验证仍需独立能力和批准；快照刷新、撤销或过期会阻止依赖它的新准备和派发准入。验证、证据和引用同事务提交，并包含真实进程退出恢复测试。

[任务关闭](spec/task-close-profile.md) 由宿主事先登记业务范围和完成合约，再通过认证 close API 请求评估。Completed 同时要求业务、结算和义务满足；工作流 Finished、Unknown 或仅完成责任转交均不等于 Completed。迟到争议可更新当前状态，历史关闭回执保持原样。

工作流 v0.5 可通过 Close 指令获取 TaskReport，结果与检查点共同提交。TaskReport 不能作为授权引用；Pending 后仍可继续 Reconcile 并再次 Close。v0.4 文件和旧检查点继续按原摘要恢复。完整验证另生成 `close-workflow-demo/` 的三条可审阅轨迹。

v0.6 增加 Validate：从候选与宿主 Context 产生 Evidence，并与节点检查点共同提交；生成证据仍不能替代能力或批准。v0.4/v0.5 保持独立版本与原摘要。`close-workflow-demo/` 的三条轨迹现在都从零 Evidence 开始，覆盖验证到业务关闭的持久链路。

[在线能力委派](spec/delegation-profile.md) 通过 schema v8、认证 delegate API 和工作流 v0.7 Delegate 实现。子权限只能衰减，Prepare/Dispatch 复检完整父链；消费同时约束租户、订单和所有祖先/子额度。撤销不清除 Unknown 预留，迟到费用继续结算。验证另输出 `delegation-model.json` 的两个变异反例，以及 `delegation-demo/` 的父子工作流 IR、兄弟竞争、父撤销与恢复轨迹。完整 P7 证明和子任务自动监督交接尚未完成。

## 使用边界

两个后端的 `.trusted` 只模拟宿主控制面；持有同一 Python 对象的代码能访问它。直接调用内核时，`subject` 参数是假定已经认证的身份；本机 HTTP API 则从服务端凭据记录取得主体。随机操作键和许可引用不能替代客户端认证。不能将内核对象、适配器对象或 Python 解释执行权限交给不可信 Agent，然后声称已经实现 R01/R16。正式服务仍需生产身份接入、签名验证、工具凭据隔离和出口控制。

内存后端的 `RLock` 只给出本进程原子性，进程退出后不可恢复。数据库后端在每次事务内从持久行恢复状态，使用 SERIALIZABLE 和租户 authority guard 行锁；子进程退出不删除已提交账本、去重记录、事件或待发送任务。模拟器仍在独立内存对象中，默认永久保留去重和墓碑，可选有限保留模式验证过期及迟到报文；远端服务崩溃、真实服务保留契约、数据库旧备份恢复尚未验证。

政策 HMAC 密钥在内存后端随机生成，在数据库后端保存于测试数据库的 authority 行，不写入证据目录；它验证本地 authority 域，不能替代用户身份、公钥发布者身份或真实人工审阅。宿主 `seal_policy` 是明确的受信入口，不能暴露给 Agent。默认合约与假设使用逻辑测试时钟；密钥隔离、轮换及生产有效期管理未实现。

模型与模拟器分别维护真实效果，不从本地成功标记推导现实成本。费用超出承诺时仍记录实际费用，冻结新支出并保持 `Disputed`；普通后续回执不能自动消除争议。事故处理、解除隔离和人工核对接口尚未实现。

schema v9 的[认证网关拒绝记录](spec/durable-rejection-profile.md)在业务回滚后、同一租户 guard 内保存事件，并限制租户总量与主体窗口速率。审计不可用返回 503，旧历史不补写；完整系统覆盖和归档仍待完成。

schema v10 的[事务审计序号](spec/audit-counter-profile.md)使用每租户计数行分配，计数与事件随事务回滚；检查历史前缀、表与载荷投影，并拒绝损坏历史升级。

[审计快照与离线校验](spec/audit-export-profile.md)提供宿主只读分页、原始 payload 哈希链及外部检查点比对；内部一致性不代表真实性或业务语义重放已通过。

离线校验可增加 `--check-payloads`，按[当前 53 种事件载荷配置](spec/audit-payload-profile.md)检查精确字段与格式；旧记录不改写。`verify.cmd --audit --smt --m1` 冻结定向审计集成、快照演示与既有门槛检查，报告明确其并非全量数据库测试。

新观察日志使用[结构化事实](spec/observation-fact-profile.md)，记录原始观察、有效费用向量和冲突时的原事实摘要。旧 repr 日志仍保留，离线报告分别计数新结构化与旧不透明观察；尚未完成跨事件语义重放。
