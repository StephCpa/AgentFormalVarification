# P7 委派组合核心的形式化设计与证明范围

证明源为 [Delegation.lean](../formal/lean/Delegation.lean)，使用固定 Lean 4.33.0 和 Std。该文件建立一个单租户、单共同根的在线委派抽象模型；原有 [Core.lean](../formal/lean/Core.lean) 继续独立检查 P1/P2/P3。这里的 P7 核心结论不是 Python/SQL 到模型的精化证明，也不是完整生产系统 P7 已完成的声明。

## 权限和链

Capability 的结构包含 id、tenant、subject、订单/工具/效果谓词、单次金额、有效期、累计额度、epoch 和是否允许委派。资源集合用 Nat 上的谓词表示，证明不依赖有限枚举。实际字符串 ID 到模型 ID 的无碰撞编码仍属于精化义务。

Attenuates child parent 要求租户和 epoch 一致，订单/工具/效果分别包含于父权限，金额、期限和额度均不扩大。它允许子主体变化；主体认证不是集合包含关系的一部分。

Chain 从根开始逐层扩展，每条边要求父能力允许委派、子能力满足衰减，且子 id 和 subject 都不在祖先路径中。路径按叶到根排列。该构造可表示任意有限深度，包含实现限制的 8 层子集；chain_ids_unique 和 chain_subjects_unique 证明路径没有重复身份。模型没有证明持久注册表的签发过程、签名或查询正确性。

|定理|结论|必要输入|
|---|---|---|
|P7_scope_transitive|叶能力衰减于路径中的每个祖先，包括根|由 Chain 构造的合法路径|
|P7_ancestor_revocation|路径任意祖先的撤销位为 true 时，CanAdmit 不成立|该祖先确实在当前检查路径中|
|revoked_root_blocks_descendant|根被撤销时，该根下的当前准入判定失败|当前撤销映射与 CanAdmit 门禁|

Live 检查撤销位、主体启用状态、epoch 与有效期。CanAdmit 还检查请求租户/主体、订单、工具、效果原子和金额上限。当前认证、时间和撤销映射是显式输入，不由本文件证明其外部真实性。实现中一次退款要求多个效果，模型按效果原子表达检查；尚未证明实际效果集合检查与此谓词等价。

撤销定理是当前准入谓词的结论，不是完整时间轨迹中的撤销持久性或线性化证明。数据库 guard 排序由运行测试支持，尚未在 Lean 中建立实现对应关系。

## 账本、额度绑定与转移

Entry 对应一个逻辑操作，保存唯一操作 ID、叶能力、冻结祖先路径及其 Chain 证明、累计 spent 和 remaining。exposure = spent + remaining。模型中的 spent 包含所建模的全部费用，但没有把真实远端成本映射到该数字。

load(account, ledger) 对每条包含该账户的操作累计 exposure；total(ledger) 对每条操作累计一次 exposure。root_counts_once 证明共同根的 load 等于 total，而不是把根和各级额度视图相加。Unique 证明逻辑操作 ID 不重复；这不替代远端幂等协议证明。

limits 是不随该段执行变化的账户限额映射。Bound 要求每个路径节点的限额恰好等于该能力声明的 quota。预留时检查新路径的 Bound，结算和保留转移保持已有绑定；避免仅证明一个与能力额度无关的任意数字上界。

|抽象转移|守卫与更新|实现对应位置，尚未机械证明|
|---|---|---|
|reserve|新操作 ID、初始 spent=0；对路径所有账户检查 load + 新 exposure ≤ limit；原子加入操作|runtime.prepare、delegation.admission 与 PostgreSQL 同事务保存|
|settle|只更新同一操作的累计费用和剩余承诺；费用单调，newSpent + newRemaining ≤ oldExposure|TrustedPlane.observe 的正常、未违约费用分支；Abort 是零费用释放特例|
|retain|账本不变|Delegate、Unknown、重放、撤销、责任转交等不直接释放费用的抽象投影|

settle 的数值条件只证明本地承诺守恒。为何观察权威、何时可以把 remaining 置零、实际费用是否覆盖完整，仍须 P4 适配器与观察语义证明。未知状态不能擅自套用结算转移的正确性，需要实现精化来建立命令与抽象转移的对应。

|定理|结论|
|---|---|
|step_safe / step_unique / step_bound|合法单步保持各账户安全、逻辑操作唯一及额度绑定|
|run_invariants|任意有限合法执行保持各账户上界与操作唯一|
|P7_shared_budget|任意有限合法执行的总承诺不超过共同根账户限额，操作 ID 无重复|
|P7_composition|所有最终操作的叶权限包含于根；每个相关祖先账户不超过该能力声明的 quota；总承诺不超过 root.quota；逻辑操作 ID 无重复|

组合定理要求初态安全、唯一且额度绑定正确，limits(root.id)=root.quota，并给定合法 Run。它不要求兄弟额度之和不超过根额度；实际消费通过共同根门禁约束，因此两个子额度各 80 可以共享 100 的根额度。多棵委派树共用租户账户、订单本金与总费用的不同映射、预算政策更新仍需进一步组合。

运行时的“新子额度不超过父当前剩余授权”比 Chain 的静态额度不扩大条件更强。本文件证明消费阶段的共同账本上界，没有证明签发服务的全部动态配额守卫。

## 非空实例与反例条件

同一文件包含可检查实例：

- 根权限允许两个订单，两个子能力分别限制到一个订单；根额度 100，两个子额度各 80。
- example_nonempty_run 构造一笔 60 的真实非空抽象执行；example_root_bound 将组合定理实例化到该执行。
- example_rejects_second_reservation 证明再预留 60 不满足共同根门禁。
- example_partial_then_final 构造累计 20、剩余 40，再累计 60、剩余 0 的合法执行。
- example_can_admit 构造一个实际可准入请求，避免所有请求均拒绝的空洞结果。
- example_root_revoked_leaf_not_revoked 检查叶自身未撤销、仅根被撤销时仍不能准入。

这些是 Lean 内核检查的具体命题。Python 的 40 状态、60 转移委派模型继续提供独立有界证据，两者不是自动生成的同一语义，也没有据此声称跨后端精化完成。

## 构建门禁与当前依赖

verify.cmd 增加 lean-delegation 阶段。门禁要求编译成功，九个指定主定理/实例均输出真实形状的公理依赖记录，记录不得缺失或重复，不允许 sorryAx、占位证明警告或白名单之外的公理。它不以日志中偶然出现目标名代替证明输出。

当前 P7_scope_transitive 依赖 propext；P7_ancestor_revocation 无公理依赖；P7_shared_budget 和 P7_composition 依赖 propext、Quot.sound。它们都是 Lean 标准逻辑依赖，没有把外部身份、适配器、SQL 或公平性写成自定义公理。原始编译日志与解析后的 proof_gate 一同进入 verification.json。

新增门禁测试覆盖正常声明、缺失/伪装名称、重复/损坏记录、自定义公理和 sorryAx。后续如改变证明依赖，应审阅实际输出并更新显式门禁，不能用通配接受绕过失败。

完整 P7 仍缺：实现与注册表精化、真实认证与撤销顺序、动态签发配额、跨树共享预算组合、费用观察及 P4 桥接、部署全中介。时序活性、子任务责任交接、崩溃恢复 P8 和一般信息流不在本文件结论内。
