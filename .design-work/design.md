# AVCL 形式化验证语言与执行系统详细设计

版本 0.1  设计基线 2026 年 10 月 25 日

本设计面向语言、验证器与运行时实现人员，将 AVCL 的研究方案落实为可分工实现、可审计和可逐项验收的技术规格。系统的核心任务是：让每次外部动作绑定可核验的授权，在并发与故障下保留完整资源承诺，并对无法确认的效果持续承担核对责任。首个业务贯穿案例采用企业订单退款。

推荐先实现统一核心 IR、参考解释器、共享持久账本与受控适配器，再接入 Python 嵌入接口。独立文本语言是后续前端，不作为首个原型的前置条件。机械化证明选择 Lean 一条主线；SMT 用于局部验证条件，TLA+ 用于有限并发和故障模型探索。各工具的证据范围独立记录。

本文中的“必须”定义实现约束，“建议”定义可调整的工程选择，“目标”定义尚待实现与验收的事项。文中的协议、伪代码、数据模式和性能值均为设计规格；不表示编译器、执行器、机械化证明或性能测试已经完成。

## 阅读导航

第 1 至 3 章给出范围、需求追踪和系统边界。第 4 至 8 章定义语言、类型、核心 IR、编译与验证。第 9 至 16 章定义授权绑定、操作状态、事务、恢复和适配器。第 17 至 20 章定义多 Agent、信息流、服务接口与部署。第 21 至 24 章给出完整案例、证明义务、测试验收和实施拆分。附录提供文法、事件样例、风险决策及参考资料。

## 1 设计范围与基线决策

### 1.1 与总体方案的关系

总体方案 [S0] 第 2 至 9 章是本设计的需求依据；第 10 章的有限模型结果属于既有材料中的结果，本设计不将其列为新增验证。沿用八类对象、仿射许可、结果未知保留预算、派发准入时点撤销以及按性质出具证据包的原则。

详细化增加四项必要区分：业务动作键与网络幂等键不同；操作结果与财务结算状态不同；本地派发准入与实际发送不同；任务完成与监督义务履行不同。这些区分用于消除恢复和重试中的语义空隙，不改变总体目标。

### 1.2 版本能力与交付顺序

|层次|纳入范围|退出条件|
|M1 核心闭环|固定政策、单租户模拟服务、授权绑定、原子预留、结果未知、确定性解释器|授权与预算的模型及参考实现一致|
|M2 故障闭环|权限 epoch、政策更新、对象版本、崩溃、重复消息、核对监督|故障注入用例全部满足预期|
|M3 组合扩展|多租户、子 Agent 衰减、时序义务、出口信息流|组合测试与部署边界审计通过|
|后续研究|完整非干扰、概率风险、复杂分布式活性、任意脚本效果|单独定义模型和证据后立项|

M1 不得把未知结果直接当作失败释放资源。M2 之前不对真实支付或其他不可逆生产接口声称完成部署验证。M3 不扩张为任意 shell 或浏览器动作代理；新增工具必须先完成效果覆盖设计。

### 1.3 推荐技术基线

参考解释器与 Python SDK 使用同一组不可变核心数据类型。生产原型仍可采用 Python 服务，避免在语义稳定前维护两套语言实现；只允许固定版本的纯表达式解释器进入关键路径，禁止执行用户提供的 Python 回调。后续是否引入 Rust 内核由性能和审计结果决定。

持久层建议采用 PostgreSQL 18 系列的受支持补丁版本，在实现启动时锁定具体版本与镜像摘要。关键事务使用 SERIALIZABLE 隔离和一致行锁顺序；序列化冲突需要从头重试整个事务 [S2]。消息队列仅负责唤醒，数据库 outbox 和监督任务表是持久事实来源。

Lean、SMT 求解器、TLA+ 工具链及前端依赖在实现仓库的锁文件中固定版本，不使用浮动 latest 标签形成证据包。SMT 无独立证书检查时，将对应求解器及编码器列入可信计算基。Lean 内核检查证明项的机制可支持小核心证明，但不会自动验证 Python 或数据库实现 [S5]。

## 2 需求编号与追踪矩阵

每项硬需求同时关联原始位置、实现位置和反例测试。条款源码携带 requirement_id；生成的 IR、诊断、证明义务与审计事件继承该编号。验收时必须能从需求追踪到执行门禁，也能从拒绝事件反查到需求。

|编号|需求及原文依据|详细设计位置|验收用例|
|R01|候选不能构造权限与证据 3.1|4 5 9 章|T01 伪造授权|
|R02|完整参数与身份绑定 5.1|9 11 章|T02 批准后改参|
|R03|对象版本与远端条件 5.1 5.2|9 16 章|T03 旧版本提交|
|R04|并发预算及多账本原子预留 5.3|11 12 章|T04 并发超额|
|R05|未知结果保留预留 5.2 5.3|10 13 章|T05 回执丢失|
|R06|撤销和更新有明确时点 5.4|14 章|T06 撤销竞态|
|R07|取消与补偿不擦除效果 5.5|13 16 章|T07 取消竞态|
|R08|仿射许可和持久防重放 3.2 4.5|5 10 11 章|T08 双派发|
|R09|有限纯谓词和分级验证状态 3.3 3.4|4 7 8 章|T09 不支持语义|
|R10|时序义务持久化 3.4 7|15 章|T10 截止与重启|
|R11|权限衰减与共享预算 6.1|17 章|T11 委派越权|
|R12|上下文及日志等出口检查 6.2|18 章|T12 隐私流向|
|R13|同一 IR 驱动多种产物 8.1|6 7 章|T13 跨后端一致|
|R14|假设失效冻结相关危险动作 8.3|8 12 16 章|T14 适配器违约|
|R15|非空性与独立真实状态 oracle 4.4 11|8 22 23 章|T15 空洞通过|
|R16|实际效果全中介及事件真实性 2.3|3 20 章|T16 绕过执行器|

自然语言要求不能仅靠回译自动验收。需求负责人核对正例、反例、观察范围和环境假设；安全策略发布者独立于候选计划生成器。若总体方案条款发生变化，先修改该矩阵与政策版本，再更新生成产物。

## 3 总体架构与可信边界

### 3.1 组件与调用关系

!ARCH!

设计上划分控制面与执行面。控制面发布不可变政策包、适配器注册项、验证器注册项和签发权限。执行面从候选进入校验、预留、派发和核对流程。Agent 进程仅持有访问网关的身份凭据，工具凭据只交给隔离的适配器进程。

|组件|输入与输出|关键责任|
|规范前端|规范或嵌入构造器 → Typed AST|解析、名称、类型、效果与资源使用|
|核心编译器|Typed AST → Core IR 与证据清单|分类、生成 VC、监控状态和执行检查|
|验证服务|VC 与模型 → 分项报告|可满足性、证明目标、边界与假设|
|授权内核|候选与凭据 → PermitRef 或拒绝|绑定、证据引用、预算与版本原子检查|
|执行网关|PermitRef → OperationRef|消费许可、持久派发意图、限制出口|
|工具适配器|已冻结请求 → 回执或不确定观察|实际请求一致性、费用、远端协议|
|监督器|未决操作 → 核对或转交事件|恢复、截止时间、重试额度、责任持久化|
|账本与审计|事务状态 → 顺序化事件|防重放、资源守恒、可重放状态投影|

### 3.2 信任分层

不可信层包括模型、提示、普通文档、候选规范和未经认证的工具内容。候选规范只有经过授权发布流程才能成为政策。注册验证器只能签发其获准的 predicate_id，不能通过返回 true 签发任意命题。

本地可信层包括身份和能力服务、规范化器、检查器、账本事务、出口网关及其部署配置。远端条件更新、幂等与真实费用上界属于外部协议假设；每项假设必须有所有者、证据、到期日和失效处置。数据库管理员与适配器维护者属于特权主体，不能用语言内证明假装其不存在。

### 3.3 边界失效时的行为

执行面无法读取当前权限版本、账本不可用或政策包校验失败时，不接纳新的危险派发。已在途操作保留账本并由恢复流程处理。核对出口可使用单独的最小只读能力继续查询；若查询也无法认证，则保持 Unknown 并记录处置责任。

审计日志中不写“已授权”自由文本作为事实依据。事件必须由完成事务的组件生成，并绑定事务序号、政策摘要及操作标识。数据库事务内写入事件/outbox；外部审计存储不可用时暂停导出，不把未持久化日志误当作可恢复状态。

## 4 语言核心与表达式语义

### 4.1 最小可实现语言

首版语义载体是 Core IR，Python 构造器只是受限 AST 构建器。金额、集合边界、身份和工具版本均显式类型化。表层文本语言与 Python 接口都不允许通过任意代码执行扩展硬门禁。

|类别|首版支持|拒绝或单独处理|
|值类型|Bool 有界 Int Id Enum Record Option 有界 Set|浮点金额、动态对象反射、任意函数值|
|表达式|字段访问、逻辑连接、比较、常数乘法、加减、成员关系|变量乘变量、递归、无界量词、网络查询|
|命令|propose validate prepare dispatch reconcile delegate close|任意 eval、隐式工具调用、直接构造授权|
|条款|requires invariant forbid before owe success assume|未分类的通用时序公式进入硬门禁|

整数在逻辑层使用数学整数；每个业务类型声明取值范围。IR 求值先检查范围，超范围返回 EvaluationError，不采用机器回绕语义。金额在 API 中采用十进制整数字符串，在内核中解析为整数，并携带币种和 scale。CNY 分与 USD 分不能直接相加，汇率转换是独立受控动作。

所有表达式必须总定义。Option 值先匹配再访问；除法和取模不属于首版核心；有界集合必须声明最大基数。布尔运算固定为左至右短路求值，但类型检查要求各子式在其守卫条件下有定义。求值错误、缺字段、未知枚举都不能解释为通过。

### 4.2 条款类别

safety 条款在动作准入时拒绝违规转移，如缺少批准或超过预算。bounded_obligation 在触发时生成持久义务，如 Unknown 后 24 小时内完成核对或转交。completion 在显式关闭任务时判断业务成功。assumption 记录外部依赖及可观测违约条件。statistical 不参与硬授权，只生成独立评估请求。

原文中的 before 和 on owe 降为明确的事件模板，首版不声称对任意 LTLf 公式进行可强制性判定。before 仅检查已持久化的关联事件或当前有效证据；“曾批准过”仍必须满足当前请求、版本和有效期匹配。

### 4.3 资源限制

建议初始配置为单模块最多 200 条条款、单谓词最多 1000 个 AST 节点、集合最多 64 个元素、单请求规范化字节不超过 64 KiB、委派深度最多 8。它们是防止资源滥用的起始值，需在第 23 章基准中验证和调整。

达到未决操作或义务容量上限时，拒绝建立新的相关实例，不删除旧义务。编译失败给出源码位置、需求编号和不支持的构造。纯谓词运行超限归为 UnknownEvaluation，并拒绝当前派发；不得自动改用模型判断。

## 5 类型 效果与资源使用规则

### 5.1 八类对象的运行表示

|对象|核心字段|构造及使用限制|
|Candidate A|payload source label schema_id|解析器可构造，不携带授权|
|Labeled T|value confidentiality integrity provenance|标签传播器合并上下文依赖|
|Evidence P|predicate_id subject_digest versions issuer scope|注册验证器签发，引用验证结果|
|Capability E|subject scope effects expiry parent epoch|授权服务签发，委派只能衰减|
|Permit A|permit_id op_id binding_digest expiry status|授权事务签发，一次性准入|
|Operation R|op_id execution_state settlement_state revision|执行器创建，不授予新的调用权|
|Obligation|trigger due owner status requirement_id|监控事务创建，监督器接管|
|Contract|policy_id version ir_digest assumptions|签名发布，内容不可原地修改|

API 返回的是对象引用和可见投影，不把客户端提交的 JSON 当成可信对象。服务端加载持久注册记录并验证租户、主体和版本。引用泄露也不能绕过主体绑定；跨服务传输的签名封套只证明来源，账本状态仍决定其是否已消费。

### 5.2 仿射规则

可复用变量环境 Gamma 与仿射资源环境 Delta 分开。prepare 返回 Permit；dispatch 从 Delta 消费对应许可并返回 Operation。分支合流只保留所有分支都未消费且类型一致的资源；循环不能隐式复制 Permit。允许放弃 Permit，但放弃变量不等于释放预留，必须调用取消未派发准备的受控操作。

只读 Capability 可按政策复用。一次性批准令牌在 prepare 时绑定唯一 op_id，不能绑定两个待执行操作；Prepared 被安全撤销后仍不自动重置批准令牌，需新签发或明确恢复授权。普通 Capability 的使用次数和累计额度也由账本核验，不依赖 Python 对象引用计数。

### 5.3 效果检查

工具效果集合至少为 Read、Write、Disclose、InvokeModel、Spawn、ResourceConsume。函数效果是所有控制分支效果的并集。工具的辅助查询、遥测及重试费用纳入相应效果声明；不能把“校验器”命名当成无副作用证明。

纯校验器只检查已有快照。需要远端事实时，先通过受控 Read 操作获取有来源和版本的快照，再由纯验证器产生 Evidence。propose 展开为上下文标签检查、InvokeModel 预算预留、模型调用、结果核对和候选解析；模型超时但费用未明时遵循同一未决结算规则。

### 5.4 类型诊断示例

E-TYPE-101 表示 Candidate 不能用于授权；E-AFFINE-201 表示许可被再次消费；E-EFFECT-301 表示存在未声明出口；E-LABEL-401 表示数据机密级别高于接收者；E-PRED-501 表示硬谓词包含非总定义表达式。诊断含 source_span、requirement_ids 和最小结构化反例，不直接暴露秘密谓词的具体取值。

## 6 统一中间表示与制品格式

### 6.1 Core IR 结构

IR 是带版本的不可变结构树，不接受可执行字符串。表达式节点使用显式 tag，例如 Eq、And、Field、Const、Add、InSet；Field 只能访问符号表中已经解析的字段。所有节点都有稳定 node_id 和源码映射。

|字段|类型及约束|用途|
|ir_version semantic_version|受支持的精确版本标识|拒绝不兼容语义|
|module_id policy_version|非空标识及递增版本|政策依赖与发布|
|types tools validators|已解析定义列表|类型和适配器绑定|
|clauses|id kind expr event_selector requirement_ids|条款分类与追踪|
|workflows|基本块及显式控制边|效果和资源使用分析|
|assumptions|id authority scope validity monitor|保证前提与失效处置|
|limits|整数范围和集合及实例上限|资源约束与模型边界|
|source_map|node_id 到原文位置|诊断及审计解释|

将执行语义部分与展示部分分离。semantic_digest 覆盖类型、条款、工具、假设和限制；source_map、显示名称单独计算 source_digest。签名清单同时绑定二者及编译器版本。编辑注释不改变执行语义，但整个发布包仍有唯一 artifact_digest。

### 6.2 发布包

政策包包含 core.ir.json、manifest.json、checks.bin、monitor.json、vc/、schema/、source-map.json 和 verification-report.json。manifest 列出每个文件摘要、生成器版本、依赖适配器与内核版本、保证配置和签名。加载器必须全部校验后原子切换活动版本，不能单独替换检查器文件。

IR 不包含工具密钥、真实用户数据或可复用批准。证明实例若需要真实对象摘要，单独生成受限证据包。包中的 Unsupported 或 Unknown 报告不因为其他目标通过而被覆盖；发布规则按条款要求判断能否启用。

### 6.3 兼容策略

语义主版本变化要求重新编译、重新出具证据并明确迁移在途实例。新增展示字段可使用向后兼容扩展；新增枚举值必须显式协商。旧执行器遇到未知效果类型或状态时停止相关执行，不按最相近旧状态解释。

历史操作持有其原始请求、适配器协议版本及政策快照引用。安全修复可将旧适配器置为 quarantine；此时禁止新发送，保留兼容的只读核对路径，不能为方便升级重写历史摘要。

## 7 编译流水线与确定性检查

### 7.1 编译阶段

|阶段|输入到输出|失败条件|
|解析与解析名称|源码或构造器 AST → Resolved AST|重复定义、未知符号、非法默认值|
|类型和效果检查|Resolved AST → Typed AST|类型冲突、资源复制、出口未声明|
|语义限制检查|Typed AST → Core IR|非终止谓词、不支持算术、缺范围|
|条款分类与依赖|IR → 检查与义务依赖图|把 completion 用作实时证明|
|验证条件生成|IR → SMT 公式及状态模型|编码失败或缺失前提|
|运行产物生成|IR → 监控器和门禁|生成器不支持目标版本|
|一致性与打包|全部产物 → 签名候选包|目标摘要不一致、缺报告|

每阶段输出诊断和输入摘要，失败不会发布半成品。对同版本输入与同一工具链要求语义产物确定；构建时间等易变字段保存在外部构建记录，避免污染语义摘要。

### 7.2 门禁求值顺序

先验证调用者身份和租户，再加载不透明引用；随后核对绑定、版本、证据和适配器健康状态，再检查信息流和业务条款，最后在事务中检查预算及许可状态。廉价拒绝先做可以节约资源，但最终判定所依赖的可变事实必须在事务中复检。

门禁不发起求解器调用。能够直接解释的有限纯表达式编译为确定性检查；离线 SMT 结果只在其前提、版本和证明依赖均适用时提供额外证据。若某硬条款既无获准的动态检查也无有效静态证据，编译或发布失败。

### 7.3 独立一致性验证

参考解释器直接读取 IR，生产门禁读取生成检查产物。测试生成合法输入、边界输入和状态转移，比较 allow/deny、状态变化和事件序列。对预算、时间、币种、证据版本和分支合流进行定向测试。

一致性测试不能证明全部编译正确性。正式目标是对每个受支持构造给出解释语义，并建立生成器保守性：生成检查允许的动作必须为核心检查允许。初期未完成的部分在证据包中标记为测试支持，而非机械化证明。

## 8 验证结果 假设与非空性检查

### 8.1 三套不可混用的状态

|维度|枚举|含义|
|求解器|Sat Unsat Unknown Timeout Error|某个具体编码的查询结果|
|性质证据|ProvedUnder BoundedChecked RuntimeSatisfied Pending Violated AssumptionBroken Unknown|对单项性质的证据状态|
|外部操作|Prepared Dispatched Confirmed RejectedNoEffect Unknown PartialEffect AbortedBeforeDispatch|实际效果的本地认知|

操作 Unknown 不等于逻辑性质被反驳；求解器 Unsat 不等于操作成功；任务 Pending 不等于安全已经验证完毕。接口禁止用一个 verified 或 success 布尔字段代替这些维度。

### 8.2 验证条件

局部保持查询为 P AND T AND NOT Q。Unsat 只有在编码健全、背景假设适用且求解器可信时，才支持该转移不存在违反 Q 的实例。Sat 提供模型，但过近似模型需要判断是否能对应真实轨迹；Unknown、Timeout、Error 不建立证明。

配套检查包括 P 可满足、初态存在、触发器可达、至少一个允许动作存在以及完成目标在声明边界内可达。硬策略允许有意拒绝某些动作，但“全部拒绝”的系统不能通过任务可用性验收。假设冲突必须报告最小相关条款集合，由政策作者修订，不自动删掉安全要求。

### 8.3 证据包

每项 PropertyReport 包含 property_id、requirement_ids、status、assumption_ids、spec_digest、ir_digest、checker_build、tool_versions、bounds、witness_ref、runtime_obligations 和 checked_at。BoundedChecked 必须写主体数、操作数、消息次数、故障次数和有限常量域；不显示“系统已验证”总徽章。

证据有效性依赖图将对象版本、权限 epoch、政策、适配器、验证器和证明内核分开。变更只使相关节点失效；失效证据保留供历史审计。若事实观察与假设矛盾，生成 AssumptionBroken 事件并冻结依赖该假设的危险操作。

### 8.4 运行观测的限制

不能观测到的假设仍是显式假设，例如远端内部事务真正原子。通过适配器测试或供应商文档只能提供契约依据，不等于数学证明。运行监测能发现重复效果或超额费用时，必须保留违规证据并降低相应保证，而不能仅删除异常响应。

## 9 请求规范化与授权绑定

### 9.1 三种标识

business_action_key 表示业务语义上的同一动作，例如租户、退款申请单号和动作种类的组合。其唯一性由业务系统签发的申请标识支撑；不能让模型随意生成新键绕过重复退款限制。相同订单可以合法多次部分退款时，使用独立退款申请并维护订单累计上限。

operation_id 表示一次逻辑外部操作，由内核分配并用作远端幂等键的来源。client_request_id 表示客户端提交请求的重试身份；键在租户、主体和接口范围内唯一。同一 client_request_id 不同摘要返回冲突，同摘要返回已有操作。三个标识都不可代替另外两个。

### 9.2 规范化过程

先用指定 schema 严格解析，拒绝重复 JSON 键、未知字段、非法 Unicode 和模糊数值；再补全唯一默认值、校验币种与资源标识；最后序列化规范化结构并计算摘要。JCS 可提供确定性 JSON 表示 [S1]，但路径别名、URL 重定向和业务标识等语义规范化仍须由适配器定义。

金额采用规范十进制整数字符串，例如 5000 分序列化为 "5000"，禁止前导加号、前导零和小数。主体及订单标识使用权威服务提供的稳定 ID；任意 Unicode 文本不擅自进行等价折叠。v0.1 不接受用户提供的任意 URL 或文件路径作为高权限目的地。

绑定摘要采用带域分离前缀的 SHA-256，输入包括租户、主体、任务、业务动作键、operation_id、工具标识、适配器版本、schema 版本、完整规范化参数、对象版本、政策摘要、权限 epoch、证据集合摘要、各账本费用上界、有效期和披露接收者。摘要算法和规范化版本也写入封套。

### 9.3 批准与证据

批准对象绑定业务请求摘要和用途，必要时在准备阶段绑定 operation_id。初始批准若尚无 operation_id，必须由内核将其一次性消费到指定操作；不得在多个准备事务中复用。批准界面显示付款来源、退款目的地、金额、费用上界和适用期限，不显示模型自行生成的描述替代真实字段。

Evidence 包含 predicate_id、subject_digest、object_versions、issuer、verifier_version、assumptions、scope、issued_at、not_after 和撤销引用。验证器签名只证明它对指定命题作出断言；结构证据不能满足退款资格或身份认证谓词。Evidence 通常可复用，但必须在每次派发准入时检查仍然适用。

### 9.4 实际请求必须从冻结数据生成

适配器从已持久化的 canonical_request 重建请求，不接受 dispatch 时附加新参数。发送前校验 wire_request_digest 与适配器声明的映射规则；默认值、序列化器、远端 endpoint 和凭据作用域均固定。禁止自动跟随跨域重定向，除非其接收者已纳入披露授权和契约。

HTTP 条件请求必须由远端在应用操作前检查 [S3]。客户端读到 order_version=v7 后本地比较一次，只是证据检查，不能替代远端条件执行。若 API 不支持所需条件，则该工具不进入要求此保证的严格配置。

## 10 操作状态机与任务状态

### 10.1 生命周期

Proposed 是候选阶段，不占用预算。prepare 成功后同时创建操作、许可和预留，进入 Prepared。dispatch 的本地事务消费许可并创建 outbox，操作进入 Dispatched；这一名称表示已接纳派发，尚不证明网络已发送。实际发送由 attempt 记录描述。

!STATE!

|当前状态|事件及条件|下一状态|资源处理|
|Prepared|准入检查通过并提交|Dispatched|保持全部预留|
|Prepared|撤销或到期且确定未曾准入|AbortedBeforeDispatch|原子释放全部预留|
|Dispatched|可信业务成功观察|Confirmed|费用终局时结算，否则保留尾部预留|
|Dispatched|可信终局无效果观察|RejectedNoEffect|结清费用后释放，未知费用继续保留|
|Dispatched|超时或恢复时无法确定|Unknown|不释放|
|Dispatched 或 Unknown|可信部分效果观察|PartialEffect|结算已知部分并保留剩余上界|
|Unknown|权威核对给出终局|Confirmed 或 RejectedNoEffect|按最终费用结算|
|Unknown 或 PartialEffect|仍无法判定|原状态|保留预留并持续监督|

PartialEffect 是对原方案“部分效果独立建模”的落实。首版退款适配器要求业务原子性；如果观察到部分退款仍必须记录该事实并进入假设违约处置，不能因为严格配置不应发生就拒绝存储。其他工具只有定义部分效果的维度、成本和恢复协议后才可准入。若后续全部效果完成，可转 Confirmed；若已确证只完成部分且不会再生效，则保留 PartialEffect 并将结算转 Final，任务按完成条款判为 Failed 或 Escalated，不能改写为无效果。

### 10.2 执行结果与费用结算正交

execution_state 表示业务效果；settlement_state 为 Unsettled、Partial、Final、Disputed。账本 pending 等于所有未最终结算 reservation 的剩余上界之和，而不是仅按 Prepared、Dispatched、Unknown 三个状态求和。这一细化覆盖“退款已确认、手续费账单未到”的情况。

同一操作在一个账本上记录 upper_bound、charged_so_far 和 remaining_bound。收到可信累计费用 c 时，用 c 减去此前已记账费用得到 delta；只有回执证明剩余费用的上界可收紧，才减少 remaining_bound。费用未终局时，默认 remaining_bound = upper_bound - charged_so_far。最终费用到齐才归零。

### 10.3 任务关闭

任务状态为 Running、Pending、Completed、Rejected、Escalated、Failed。Completed 要求业务 success 条款成立，相关执行效果已有终局且任务要求的结算、义务全部满足。Escalated 只代表处置责任转移；Unknown 操作与预算不会随任务关闭而消失。

部分任务可允许“业务成功但财务待结”，这必须使用单独的业务结果字段和 Pending 总状态，不复用 Completed。远端无效果拒绝与本地政策拒绝也分别记录，避免将未发出的请求统计为服务失败。

## 11 授权和派发事务算法

### 11.1 共同事务约束

首版将同一租户的 authority_guard 作为授权、撤销、政策切换和派发的共同串行化锁；再按固定顺序锁 task、capability、operation 及按 ID 排序的 budget_account。所有写路径遵循同一顺序。初期接受租户级竞争，待证明分片不破坏时点语义后再优化。

关键事务不调用网络、不运行模型、不等待用户输入。序列化失败和死锁采用有界退避重试整个事务；事务内部不发送外部效果。响应丢失时，客户端用原 client_request_id 查询或重试。重复调用成功返回原结果，不重复增加预留。

### 11.2 prepare

预处理阶段完成结构解析、候选纯检查和证据引用准备。事务阶段执行如下步骤，任何失败全部回滚。

```text
BEGIN SERIALIZABLE
lock authority_guard; read active_policy and epoch
lookup client_request_id; if exists compare digest then return
lock capability, approval, business_action_key, budgets
reload evidence validity and adapter admission profile
check subject, scope, versions, labels, pure preconditions
for each account b: require spent[b] + pending[b] + u[b] <= limit[b]
allocate operation_id; bind approval once to operation_id
insert operation(Prepared), permit(Live), reservations(u)
increase pending in every affected account
append Prepared event and commit result for request id
COMMIT
```

多账本任一不足即全部失败，不能先占租户预算再异步占订单预算。业务动作键若已存在未决或成功操作，返回已有操作或业务冲突；不得创建一个新 operation_id 继续退款。单次准备的有效期在签发时固定，过期不自动延长。

### 11.3 dispatch

调用者只提交 PermitRef 和 client_request_id。服务器加载冻结请求，重新检查主体、权限 epoch、活动政策、证据时效、对象前提和适配器状态。对象实时变化由发送时的远端条件最终裁决，不能靠本地事务锁住远端对象。

```text
BEGIN SERIALIZABLE
lock authority_guard, operation, permit, dependent rows
if operation already admitted: return same OperationRef
require state == Prepared and permit == Live
require exact binding and versions and authorization now
require valid reservations and monitor admission predicate
set permit = Consumed; state = Dispatched
insert outbox UNIQUE(operation_id, command_kind)
append DispatchAdmitted event and required obligations
COMMIT                         # authorization linearization
```

同一操作的重复 dispatch 返回相同句柄；使用许可请求另一个动作则拒绝。事务提交后才允许 sender 消费 outbox。若提交结果对客户端未知，不能新建操作替代查询。

### 11.4 sender

sender 领取带 lease 和 generation 的 outbox 任务，持久化 attempt_id 和发送意图，再使用固定 operation_id、完全一致参数与远端条件发送。lease 只控制本地工作分配，不保证远端只执行一次。旧 worker 恢复与新 worker 重发时，必须依赖服务端幂等或远端 fencing 协议。

sender 收到响应后交给注册验证器转换为 Observation，独立结算事务应用。sender 崩溃且无法证明未发送时转 Unknown。监督器持续扫描超过适配器等待上界的 Dispatched 及过期发送 lease，在事务中登记 Unknown 并创建核对义务，防止操作永久停留在无人处理的在途状态。禁止“拿到 lease 超时就释放预留”。严格配置中没有有效远端去重契约的写操作，不允许以自动重发恢复。

## 12 持久数据模型与预算守恒

### 12.1 关系模型

所有业务表主键或唯一键包含 tenant_id；数据库访问层必须携带租户条件。敏感请求和回执使用加密对象存储，数据库保存内容摘要和受控引用。下面字段是逻辑规格，SQL 类型与索引需按最终 schema 固化。

|表|主键和关键字段|约束及索引|
|authority_guard|tenant_id epoch active_policy revision|租户唯一，串行化授权变更|
|capability|tenant_id cap_id parent subject scope epoch expiry|父链存在，作用域受限|
|evidence|tenant_id evidence_id predicate digest versions issuer|验证器白名单及依赖索引|
|operation|tenant_id op_id business_key generation state revision request_digest|动作键与代际唯一，历史不删除|
|business_action|tenant_id business_key current_op status|动作键唯一，串行化替代和防重|
|permit|tenant_id permit_id op_id status expiry binding_digest|op_id 唯一，消费 CAS|
|budget_account|tenant_id account_id currency limit spent pending|金额非负，revision 单调|
|reservation|tenant_id op_id account_id upper charged remaining|三元组唯一，非负约束|
|observation|tenant_id observation_id op_id source_sequence digest|来源及序号去重|
|outbox|tenant_id op_id kind status lease generation|操作命令唯一，待执行索引|
|obligation|tenant_id obligation_id op_id due owner status|触发键唯一，截止时间索引|
|audit_event|tenant_id sequence event_id op_id payload_digest|租户内 sequence 唯一|
|request_dedup|tenant_id subject endpoint request_id digest result|范围内唯一，保留期可配置|

business_action 保存唯一当前操作引用；operation 可保存安全取消后的历代准备记录。只有当前操作处于 AbortedBeforeDispatch，或已获得允许重新发起该业务的终局无效果证据及新授权时，才可原子递增 generation 并替换 current_op。Unknown、PartialEffect 或已成功的业务动作禁止建立替代操作。客户端请求去重仍返回原代际结果，不自动指向新代际。

业务动作键和远端幂等墓碑的保留期不能仅依据 HTTP 缓存寿命决定。即便删除含个人信息的请求正文，仍应依法按业务保留规则保存必要的不可逆摘要和防重事实；清理策略必须经过安全和业务评审。

### 12.2 多维预算

对每个账户 b 维护 S[b] + U[b] <= B[b]，其中 U 是 reservation.remaining 的总和。一次退款可以同时影响租户总支出、订单累计退款和调用次数账户；这些是不同约束维度，不能把多个账户的 spent 相加当作实际总费用。

准备时原子增加 U。派发、超时、任务暂停和监督转交都不降低 U。结算时，将 delta 加入 S，再按可信剩余上界调整 U；必须保证新增已知费用加剩余承诺不超过原来的剩余额度。所有 affected accounts 在同一事务中更新，事件包含每个账户的增量。

无效果拒绝并不意味着零费用。若服务端收取调用费，先结算费用；费用尚未确定时保留其上界。模型请求同样需预留输入、最大输出和协议允许的重试费用；没有可靠价格及输出上限时只允许明确的软预算配置。

### 12.3 超额观测不能被数据库约束丢弃

实际费用超过声明上界时，先持久化不可变原始 Observation，再在事故事务中记入真实费用，标记 account=Breached、settlement=Disputed 和 AssumptionBroken，冻结相关新支出。不得因为 CHECK(spent+pending<=limit) 导致真实费用无法入账。

因此数据库永久硬约束限定非负性、唯一性和引用完整性；正常授权路径在事务中强制预算不等式。事故路径允许如实表示不等式已破坏，但不能再声称对应现实成本定理适用。账本投影与原始观测分离，保证失败事实始终可恢复。

### 12.4 幂等结算

重复同一回执必须产生零增量。相同来源序号对应不同摘要视为冲突，进入 Disputed；不能以后到的回执覆盖先前成功事实。累计费用只允许按协议单调增加，合法退款或费用冲回使用新会计事件，不逆写历史。

定期核对 budget_account.pending 与 reservation 聚合和；不一致时冻结相关账户并从事件重建，不能悄悄取较小值。高并发实现可维护聚合缓存，但缓存不作为单独的授权事实来源。

## 13 崩溃恢复 重试与取消

### 13.1 崩溃点的处理

|崩溃位置|持久事实|恢复动作|
|prepare 提交前|没有完整操作或事务已回滚|用原请求键重试，不产生重复预留|
|prepare 提交后响应丢失|Prepared 与许可和预留已存在|返回原结果|
|dispatch 提交前|仍是 Prepared|重新检查后准入或取消|
|dispatch 提交后发送前|Dispatched 与 outbox 存在|按同一操作发送；无法确定未发送则核对|
|网络已发送回执未到|可能已产生外部效果|Unknown，保留预留并查询|
|远端成功本地结算前|可信回执可能尚未持久化|再次查询或接收重复回执后幂等结算|
|结算提交后响应丢失|Final 或部分结算已持久化|返回现状，不再次记账|
|监督器处理中重启|义务与 lease 存在|回收 lease 后续办，不重置截止时间|

Prepared 清理只有在与 dispatch 共享的锁内确认从未准入，才能转为 AbortedBeforeDispatch 并释放。Dispatched 即使本地 send_attempts 为空，也不能仅凭计数断言远端无效果；必须有可证明的发送所有权取消协议才可安全回退。

### 13.2 重试资格

每次重发要求：原 operation_id、原参数与接收者、远端去重作用域不变；去重有效期内有足够网络时钟安全余量；适配器契约允许重发；尝试次数和每次可能新增费用仍在已预留上界内。网络库的自动重试默认关闭，由 sender 统一控制。

过了去重期限只能做权威核对或转交，不得用新 operation_id 试一次。即使本地 request_dedup 已过期，业务动作键与未决操作记录仍阻止重复业务动作。允许用户发起新的业务意图必须有独立的业务确认和上限校验。

### 13.3 核对与终局无效果

核对返回 Confirmed 必须包含与操作、参数和业务版本匹配的可信效果及费用证据。返回 RejectedNoEffect 必须保证该逻辑请求未发生业务效果，而且任何排队、延迟或重复到达的同键请求均不会再生效。

普通 not_found 只表示当前查询未找到，不具备该终局含义。适配器需要远端取消墓碑、终局状态或等价协议。若远端无法保证晚到请求不会执行，继续 Unknown；重复查询次数增加不会把否定观察变成证明。

核对调用自身也有 Read、Disclose 与费用效果。监督器使用预留的维护额度或单独授权预算；维护额度不足时转交，不得绕过硬预算。核对服务不能因主要写权限撤销而获得新写权限。

### 13.4 取消和补偿

本地取消只停止后续新动作。远端取消请求是独立受控操作，可能失败、超时或收费；只有终局取消证据及费用结清后才释放原操作预留。用户界面分别显示已停止调度、已请求取消、已确认无效果取消。

补偿使用新的 operation_id 和 business_action_key，并显式引用被补偿操作；重新获得权限和成本预算。补偿成功改变后续业务状态，历史泄露、付款或政策违规仍留在实际效果轨迹中。

## 14 权限撤销与政策更新

### 14.1 撤销顺序

撤销事务锁定 authority_guard，递增相应 epoch，写撤销事件后提交。dispatch 同样持锁读取 epoch。由提交顺序定义线性化：撤销先提交则旧许可不能准入；准入先提交则原操作仍可能发送和完成。此时不再签发新的派发权，但同一操作的协议内重发可能继续。

这不是“撤销后远端绝不生效”的承诺。若业务要求更强语义，远端必须在效果提交时验证 epoch，或提供可确认的取消协议。紧急停用可暂停 outbox，但停用前已发出的请求仍进入 Unknown 核对流程。

### 14.2 旧许可处置

Prepared 许可绑定旧政策时不原地修改摘要。内核在同一事务中撤销旧许可并释放其未派发预留，重新执行 prepare 生成新的操作代际或新许可记录；业务动作键仍关联同一业务意图，并保留完整替代链。新批准是否需要再次取得，取决于批准范围是否覆盖新政策和参数。

为减少首版复杂性，默认采用取消旧 Prepared 后显式重新提交，不实现自动迁移。Dispatched、Unknown 与 PartialEffect 不进行政策迁移；只改变后续监督和允许恢复动作，并保留其准入时依据。

### 14.3 政策发布流程

候选规范编译后经过需求映射审阅、冲突及非空性检查、回归测试和签名，生成不可变包。发布事务在 authority_guard 下检查兼容性、当前 S+U、适配器依赖及在途操作处置规则，再切换 active_policy。新预算低于既有承诺时拒绝普通更新。

允许授权的过渡政策显式冻结新支出、保留旧承诺并设置新预算生效条件；它不是把违规重新命名为合规。回滚也生成新的政策版本和审计事件，不恢复旧 epoch。自动修复只能修改候选计划，不能降低已冻结硬条款。

## 15 时序义务与可信时间

### 15.1 义务模型

每次 Unknown 首次触发 O-Reconcile：在指定期限内取得终局核对，或完成被授权的人工处置交接。字段包括 obligation_id、trigger_event、operation_id、kind、due_at、owner、generation、status、evidence_ref。触发与操作状态变化在同一事务持久化，重复 Unknown 不重置 due_at。

义务状态为 Open、Claimed、Satisfied、Breached。claim lease 到期可转回 Open；业务截止时间不可延期。转交处置只有在责任方确认接收或可靠工单系统确认受理后才算 Escalated。发送通知或写入内存队列不算履约。

### 15.2 截止判定

事件逻辑顺序使用账本 sequence，时长使用进程内单调计时。跨重启不能直接复用上次单调时钟读数，因此持久化可信时间服务提供的时间区间、boot_id、触发时间和保守到期界限。无可信墙钟误差界限的部署不能承诺跨重启的严格现实时间截止。

放行时使用当前时间区间上界检查许可仍未到期；判断已确定超期使用下界。时间区间跨越截止时，停止新的时间敏感动作并标记时间不确定，义务保留。重启恢复时不重新给予完整 24 小时，而根据持久期限重建定时任务。

### 15.3 调度与背压

监督器按 due_at 分批领取义务，使用数据库 lease 和 generation 防止过期 worker 修改当前状态。截止时间只是监测和处置目标，调度器停机不能保证按期处理；恢复后记录原截止时点与实际发现时点，晚处理不抹去 Breached。

达到容量阈值时，暂停创建会产生新义务的危险任务，同时保留核对及处置通道。24 小时沿用示例业务参数，可按合约调整；它不是未经测试的可用性 SLA。完成条件与处置义务分别统计。

## 16 工具适配器契约与接入门槛

### 16.1 注册契约

|契约域|必填内容|严格配置要求|
|身份与出口|endpoint 接收者 凭据作用域 重定向规则|无法绕过网关，无自由目的地|
|请求映射|schema canonicalizer serializer 版本及摘要|绑定字段与实际发送一致|
|效果模型|成功 拒绝 部分效果 中间效果 环境交错|覆盖所有安全相关后果|
|条件执行|对象版本字段和冲突结果|远端原子检查业务前提|
|幂等|键作用域 保留期 同键异参处理 提交原子性|重复同键不产生额外业务效果|
|成本|费用维度 每次尝试上界 最坏总上界|真实费用被覆盖|
|核对与取消|查询一致性 终局证据 晚到请求语义|无效果证明排除未来生效|
|运行限制|最大延迟 时钟误差 重试次数 健康状态|超界后有明确处置|

客户端重试安全需要由服务端请求身份及其协议支持 [S4]；声明 idempotent=true 不是接入证明。适配器注册项附供应商协议证据、模拟测试、实际沙箱测试及未覆盖行为，标出哪些是可机械检查事实、测试支持或外部假设。

### 16.2 保证配置

StrictRefund 要求可信身份、完整绑定、原子预算、业务幂等、远端版本条件、终局核对及费用上界。缺少任一条件，发布器拒绝将工具放入 StrictRefund。ObserveOnly 只允许模拟或被批准的只读观测，不隐式执行严格配置的写操作。

需要软预算或不可确定效果的工具必须使用另一个显式配置，报告逐项列出被取消的保证；调用方主动选择并获得所需授权。运行时不为提升成功率自动降级。工具返回违反契约时将注册项置为 quarantine，保留只读核对能力。

### 16.3 适配器一致性测试

测试同键同参并发、同键异参、成功回执丢失、先超时后执行、对象版本冲突、延迟请求遇到取消墓碑、去重期临界点、部分费用和乱序回执。使用独立服务端状态 oracle 记录实际效果，不能只看客户端成功码。

适配器若包含支付后发邮件等中间效果，邮件接收者和披露必须进入抽象关系。无法观测或控制的中间效果要么由外部协议假设明确覆盖，要么缩减工具权限；不能用最终退款状态正确来替代过程安全。

## 17 多 Agent 委派与组合

### 17.1 权限衰减

delegate 要求父能力允许委派，并检查 child.subject 为已认证子主体、child.scope 为父作用域子集、child.effects 为父效果子集、child.expiry 不晚于父能力、child.limit 不高于父剩余授权。资源集合采用结构化 ID 集合或可判定前缀，不用任意正则表达式判断包含关系。

作用域、工具和期限检查均为纯谓词；有配额的委派分配使用账本事务。父撤销使其后代不能新派发，校验时检查完整父链的 epoch 或其可验证的汇总版本。委派链过深、循环或缺少祖先时拒绝。

### 17.2 首版选择共享账本

所有子 Agent 的消费同时检查根账户和各自子额度账户。子额度是额外上限，根账户仍约束实际总额；不把子额度总和当作新的预算。兄弟额度可配置为相互独立的硬配额，但不能绕过根账户共享串行化。

M3 首版不实现可离线支出的预算托管。若以后采用 escrow，必须新增“根可用额度、已托管额度、已消费额度守恒”证明，并规定未决子操作存在时如何回收份额。不能既在父账户预留整份额度，又把同一次子消费作为新增独立承诺重复记账。

### 17.3 因果与组合条件

事件带 parent_event_ids 和 task_parent_id 解释依赖；冲突顺序仍由资源事务决定。组合验收证明子权限包含关系、共同预算不变式和环境假设闭合。A 假设 B 安全、B 又假设 A 安全的循环不算证明；需共同不变式或带良基顺序的依赖证明。

子任务退出不代表其 Unknown 操作结束。监督所有权转交父任务或公共监督器并持久化；Agent 进程死亡不能删除责任和预算。跨租户消息必须按披露出口检查，不通过共享日志泄露资源存在性。

## 18 信息流与模型调用

### 18.1 双标签

机密性使用有序级别 Public、Internal、Confidential、Restricted，并附租户和用途集合；完整性独立表示 Untrusted、AuthenticatedSource、Validated(predicate_id)。AuthenticatedSource 仅表示来源可信，不证明任意业务事实。

组合数据的机密性取各输入及程序控制标签的上确界；完整性不因为拼接或模型生成而提升。模型输出继承完整上下文、会话记忆、检索材料和控制依赖的机密性，完整性默认 Untrusted。清除提示中的原文不清除已经派生的依赖。

### 18.2 检查出口

模型提供商、工具接收者、日志、遥测、错误响应、子 Agent 消息和人工处置系统都是接收者。发送前检查接收方获准的级别、租户、用途和数据字段。错误信息采用稳定错误码与经过标签审查的详情，敏感反例存入受限证据存储。

endorse 只能由获准验证器为指定命题提升完整性；declassify 需要独立授权，并绑定源数据范围、接收者、用途、期限和审计事件。模型给出“已脱敏”不构成 declassify 依据。

### 18.3 保证边界

v0.1 至 M3 的目标是规定出口的显式信息流约束、完整性隔离和有限控制依赖传播，不声称覆盖时间、资源消耗或终止信道的完整非干扰。随机模型的概率关系性质和差分隐私 accountant 需独立扩展，不将其埋入普通 private 标签。

## 19 服务接口与错误协议

### 19.1 API 约定

所有变更请求经认证通道提交，tenant_id 由凭据与路由绑定并交叉核对，不能信任请求正文自报。引用返回采用不透明 ID，客户端无权提供服务端状态字段。金额为字符串，时间为带时区 UTC 值和服务端签发的有效范围，关联追踪使用 trace_id。

|接口|核心输入|核心输出与幂等|
|POST /v1/candidates/validate|candidate validator_id context_refs|checked_ref evidence_refs；无授权|
|POST /v1/operations/prepare|checked_ref cap_ref approval_refs request_id|permit_ref op_id reservations；同键同结果|
|POST /v1/operations/{id}/dispatch|permit_ref request_id|operation_snapshot；重复返回原状态|
|GET /v1/operations/{id}|有权访问的操作 ID|结果、结算、监督及保证投影|
|POST /v1/operations/{id}/reconcile|request_id reason|核对任务引用，不立即承诺终局|
|POST /v1/operations/{id}/cancel|request_id cancel_kind|本地停止或远端取消操作引用|
|POST /v1/capabilities/delegate|parent_ref child_subject attenuations|child_cap_ref；配额原子检查|
|POST /v1/admin/policies/activate|signed_manifest expected_revision|新版本及生效 sequence|
|POST /v1/admin/authorities/revoke|scope expected_epoch reason|新 epoch 与撤销 sequence|

### 19.2 准备请求示例

```json
{
  "client_request_id": "req-prepare-001",
  "checked_ref": "checked-7f21",
  "capability_ref": "cap-refund-agent-a",
  "approval_refs": ["approval-refund-case-42"],
  "business_action_key": "refund-case-42"
}
```

checked_ref 指向服务端已验证的规范化请求和证据快照。business_action_key 必须与批准和权威业务对象一致。完整参数保存在服务端引用中，后续 dispatch 不接受 amount 或 destination 覆盖。

```json
{
  "operation_id": "op-42",
  "execution_state": "Unknown",
  "settlement_state": "Unsettled",
  "task_outcome": "Pending",
  "reservations": [
    {"account": "tenant-cny", "remaining": "5100"},
    {"account": "order-42-cny", "remaining": "5000"}
  ],
  "obligation_refs": ["obl-reconcile-42"],
  "property_report_refs": ["report-auth-42"],
  "revision": 4
}
```

### 19.3 错误分类

|错误码|HTTP 建议|客户端行为|
|INVALID_REQUEST|400|修正结构后新请求，保留诊断|
|AUTH_DENIED 或 SCOPE_DENIED|403|不自动重试，不提示敏感事实|
|BINDING_MISMATCH 或 IDEMPOTENCY_CONFLICT|409|停止派发，核查请求身份|
|STALE_POLICY 或 STALE_EVIDENCE|409|重新校验并取得新准备结果|
|BUDGET_EXCEEDED|409|等待额度或申请授权，不缩小报账金额|
|UNSUPPORTED_GUARANTEE|422|使用合规适配器或显式修改配置|
|DEPENDENCY_UNAVAILABLE|503|查询原请求状态后按规则重试|
|ASSUMPTION_BROKEN|409|进入处置，冻结依赖动作|

HTTP 202 表示已持久接纳异步处理，不表示业务成功；HTTP 200 的状态查询也可能返回 Unknown。错误封套包括 code、request_id、operation_ref、retry_advice、requirement_ids 和受控 detail_ref。网络断连后应先查询，不把 5xx 推断为远端无效果。

### 19.4 Python 嵌入接口边界

SDK 提供 propose、validate、prepare、dispatch、get_operation、request_reconcile 等异步方法，但 Capability、Evidence、Permit 的公开构造器不可用。对象不能通过 pickle 或字典反序列化获得服务端信任；SDK 的类型检查是开发便利，服务端再次核验才是安全边界。

SDK 的 retry helper 只复用原 request_id 和已冻结参数，不自动重新 propose。with 或 finally 块不能在异常时释放 Unknown 预留；只能请求取消本地未准入的准备或转交监督。

## 20 部署 审计与运维

### 20.1 最小部署

建议首个可复现环境包含 policy compiler、runtime API、sender、supervisor、PostgreSQL、加密证据存储和独立模拟工具服务。可以同机多进程运行，但信任边界保持进程与凭据隔离；Agent 容器只能访问 runtime API 和批准的数据入口。

使用网络出口白名单、独立服务身份和最小工具凭据落实全中介。生产凭据不能出现在模型上下文、SDK 环境或普通日志中。凭据失效、策略签名校验失败或数据库不具备所需一致性时，readiness 拒绝新写任务，健康页仍可解释故障状态。

### 20.2 故障转移

应用副本可多实例，授权账本首版保持单一强一致写主。旧主或旧 sender 必须被网络和服务身份隔离，防止双主发送。事务持久性依赖数据库配置与存储；若故障转移可能丢失已确认提交，不得声称防重放和账本不丢失。

灾难恢复可能把本地状态恢复到远端已经执行之前。恢复后进入 RecoveryRestricted，先扫描历史业务键、远端幂等记录和未决操作，完成核对再开放写流量。不能仅从备份成功启动就假定所有预留正确。若远端去重期已经过去且事实不可核对，保留挂起并转交。

### 20.3 审计与观测

每个事件记录 event_id、tenant_sequence、operation_revision、actor、event_kind、request_digest、policy_digest、authority_epoch、causal_parents、evidence_refs、budget_deltas 和时间信息。租户事件序号由事务性计数行分配并随事务回滚，不使用可能因回滚产生合法缺口的普通序列来判断事件丢失。事件链摘要及外部只追加存储可帮助发现篡改，但不能证明观察内容真实。

至少监控未知操作数与最老年龄、预留总量、义务超期、派发拒绝原因、去重冲突、序列化重试、适配器违约、审计导出积压、核对费用和实际重复效果。报警分安全事件、容量事件和外部假设违约，避免把 Unknown 总量上升直接解释为系统不安全。

### 20.4 运维规则

管理员手工释放预留属于受控状态变更，必须提供终局证据并走结算服务；没有证据只能进行明确的风险接受和保证撤销流程，不能保持“严格预算”标签。审计清理、迁移和修复都产生版本化记录，并验证未决操作及防重墓碑未遗失。

## 21 退款场景端到端设计

### 21.1 示例业务与政策

示例租户预算为 10000 分，订单可退款余额为 8000 分，本次申请退款 5000 分，最高手续费 100 分。批准指向订单原支付渠道，订单快照版本为 v7，权限 epoch 为 12。所有数值仅用于说明协议，不是实验结果。

模型读取经授权的订单上下文并提出 Candidate。订单服务提供带版本的可信快照；RefundEligibility 检查金额范围、申请状态和原支付渠道，产生特定谓词证据。批准由权限服务或获准人工流程签发，不能来自模型自填的 approved=true。

### 21.2 预算与状态演变

|步骤|租户 S 与 U|订单 S 与 U|操作与判断|
|初态|0 与 0|0 与 0|总预算和订单上限有效|
|prepare|0 与 5100|0 与 5000|Prepared，批准已绑定|
|dispatch|0 与 5100|0 与 5000|Dispatched，记录 outbox|
|远端成功但超时|0 与 5100|0 与 5000|Unknown，不能释放|
|核对成功 实际手续费 80|5080 与 0|5000 与 0|Confirmed 且 Final|
|任务关闭|5080 与 0|5000 与 0|义务满足后 Completed|

租户和订单是两个约束账户，不把两行费用合计为 10080。若手续费尚未终局，租户先确认 5000，仍保留 100 上界；订单账户可以先完成 5000 的结算。额外核对费用由单独的监督额度承担并纳入总费用核算。

### 21.3 两条关键故障路径

若 prepare 后订单变为 v8，dispatch 可能在本地检测到证据失效；即便没有及时观察到变化，远端条件更新也应拒绝 v7 请求。收到可信无效果且零费用终局证据后释放预留；若条件拒绝仍收费，先结清费用。

若远端已经退款而回执丢失，另一个 Agent 再提出同一退款申请，应命中 business_action_key 并得到原操作，不创建新退款。改用不同申请号的尝试也必须由业务授权与订单剩余额度约束，不能只靠去重键防护。

### 21.4 与总体示例的语义对应

原文 propose、validate、prepare、dispatch、reconcile 和 close 保持含义。实现中 dispatch 返回持久 Operation 快照，最终业务结果可由同步等待辅助函数返回；超时只结束本次等待，不结束操作。on Unknown owe 条款生成独立义务，不依赖原调用栈存活。

## 22 形式化模型与证明义务

### 22.1 核心状态和转移

核心状态包含 capabilities、permits、operations、reservations、budget_accounts、policy_versions、epochs、monitor_states、obligations 与 event_history。现实状态另包含远端效果、消息通道、去重记录和服务端对象版本。投影函数 abs 将实现事务和服务端观察映射到核心状态。

Step 关系包含 Prepare、AbortPrepared、AdmitDispatch、SendAttempt、RemoteApply、ObserveTimeout、ObserveReceipt、Settle、Reconcile、Revoke、ActivatePolicy、Delegate、Tick、Escalate 和CrashRecover。外部 RemoteApply 不能与本地 AdmitDispatch 合并成一个原子动作。

### 22.2 目标与前提

|目标|精确结论|必须显式携带的前提|
|P1 授权完整性|每个准入事件有匹配且当时有效的许可|不可伪造、绑定正确、共同原子时点|
|P2 许可唯一消费|每个许可至多一个成功准入|持久唯一键、状态转换原子性|
|P3 账本保持|正常转移保持每账户 S+U<=B|初态有效、预留结算守恒、更新受控|
|P4 现实成本上界|真实累计成本 <= S+U|真实费用上界、幂等与成本全覆盖|
|P5 撤销时点|撤销后旧 epoch 无新准入|撤销和准入共享顺序|
|P6 轨迹安全|实际前缀满足指定安全条款|监控健全、效果抽象、观察与环境 rely|
|P7 委派组合|子权限包含且全局额度不重复|共同账本或另证托管守恒|
|P8 恢复精化|崩溃和重放不新增未授权逻辑操作|持久状态不丢失、远端协议覆盖重发|

P3 是本地状态性质，P4 需要桥接现实。适配器违约产生的事故转移可以打破 P4 的前提；系统必须记录该事件而不是继续报告定理无条件成立。P6 需要安全相关中间效果被模型覆盖，不能只观察最后的返回值。

### 22.3 Lean 文件划分

建议依次建立 Types.lean、State.lean、Semantics.lean、Authorization.lean、Budget.lean、Revocation.lean、Delegation.lean 与 Refinement.lean。先证明 Prepare、AbortPrepared、Settle 等单步引理，再证明任意可达状态的不变式。每个 theorem 对应 property_id 和构建产物。

P4 使用单独的 AdapterSoundness 假设把真实成本映射到预留；不能将其写成无说明的 axiom 后在摘要中省略。构建检查禁止 sorry，并报告声明使用的公理与假设。编译器、解释器与 SQL 实现的精化未完成时，机械证明范围止于核心语义。

### 22.4 有限模型检查

TLA+ 模型首组边界建议为 2 个 Agent、每个至多 2 个操作、2 个账户、2 个 epoch、每操作至多 2 次发送、一次崩溃和有限费用域。故障动作包括回执丢失、消息延迟、同键重放、撤销交错、错误释放和终局拒绝。具体边界与实际状态数由工具运行后报告，不在设计中预填通过结果。

将安全检查与活性检查分开。安全不要求公平调度；最终核对需要弱公平调度、远端最终可响应或可转交等假设。加入 stuttering 后，不能用有限终止轨迹枚举宣称所有无限执行最终完成。

### 22.5 原始模型的复现口径

总体方案 [S0] 第 10 章记载正确模型为 54 个可达状态，但附件未包含其可执行源程序。本次详细设计仅将它作为待复现基线；实现时先恢复其原始动作、去重和终止计数定义，再比较数字。扩展了崩溃或重发后状态数改变是预期现象，不能为匹配 54 人为删减新行为。

## 23 测试计划与验收标准

### 23.1 分层测试

语言测试覆盖文法、名称、类型、效果、仿射分支与总定义性。语义测试比较 IR 解释器与生成检查器。事务测试在真实数据库执行并发与进程故障。适配器测试使用独立远端 oracle。部署测试验证网络和凭据隔离。证明构建与有限模型检查分别生成可复现报告。

|用例|注入或输入|必须观察到的结果|
|T01|模型提交伪造 Capability 或 Evidence|拒绝构造或引用，零外部效果|
|T02|批准 5000 分后改金额或目的地|绑定不匹配，原操作不被替换|
|T03|快照 v7 准备后远端变 v8|远端拒绝旧条件或本地先拒绝|
|T04|预算 100 并发申请 70 与 60|至多一个预留成功，无超额承诺|
|T05|远端成功并丢弃回执|Unknown 保留预留，同键恢复不重复效果|
|T06|撤销与派发交错|每次结果符合共同线性化顺序|
|T07|取消时请求仍在远端队列|无终局证据不得释放|
|T08|双进程消费同一许可|一个准入，另一个返回原操作或冲突|
|T09|非线性谓词、超时和未知枚举|显式失败，零隐式放行|
|T10|Unknown 后监督器重启和时钟回退|义务不丢失，不重置原期限|
|T11|子 Agent 扩大工具或预算范围|拒绝委派或拒绝超范围消费|
|T12|秘密通过模型上下文、错误或日志出口|按接收者策略拒绝或脱敏授权|
|T13|同一输入在两个执行后端运行|允许判定和状态增量一致|
|T14|返回超上界费用或矛盾回执|保存真实观察，AssumptionBroken 并冻结|
|T15|前提矛盾或触发器不可达|指出空洞性，不能显示完整通过|
|T16|Agent 绕过网关直连工具|部署层阻断并产生可核查证据|
|T17|在各事务提交和发送边界杀进程|无重复预留，无错误释放|
|T18|去重期限过后重发或换新操作键|禁止自动写重试，保留核对责任|
|T19|多账本第二个账户不足|整体回滚，第一账户无残余预留|
|T20|账本从旧备份恢复|进入恢复限制，核对前不开放写流量|

### 23.2 可复现要求

每次测试保存代码与政策摘要、镜像及依赖锁、种子、消息调度、故障点、完整事件序列和 oracle 实际效果。反例缩减后仍需保存原始轨迹。通过标准依据实际效果与账本事实，不能只检查最终返回 Completed。

模型故障分两组：契约内故障应保持所声明保证；契约外故障验证能否检测、保存证据及停止后续相关风险。后者检测成功不能证明原始外部效果没有违规。两组报告不得合并为一个“安全率”。

### 23.3 原型性能目标

建议固定 8 vCPU、16 GiB 内存、同机数据库、100 条政策、单次 2 个预算账户、32 并发客户端作为首组测试环境。编译耗时目标不超过 5 秒；不含外部工具和模型延迟的 prepare 与 dispatch 各自 p95 目标不超过 50 毫秒。它们是待验证目标，不是设计保证。

测量 p50/p95/p99、吞吐、事务重试率和 CPU/内存，同时设置高冲突与低冲突两组。建议测 10000 个在途操作的恢复扫描，目标 60 秒内重建监督队列；这不表示 60 秒内完成外部核对。容量到限必须表现为背压而不是丢弃状态。

### 23.4 语言价值实验

在相同适配器、政策、预算、模型和门禁语义下比较手写 Python 监控器、IR 加 Python SDK，以及可选文本 DSL。评估需求漏译、规范更新缺陷、编写时间和调试时间；安全任务完成率同时要求业务完成且整条实际效果轨迹无违规。

任务级配对并冻结攻击测试集；同一任务的重试费用计入总成本，成功、拒绝、挂起和转交分别报告。机械证明、有限模型检查、故障测试与统计基准作为不同证据层展示。若文本 DSL 不改善规范质量或维护成本，则保留 IR 和 SDK，不继续扩张语法。

### 23.5 发布门槛

M1 发布要求 R01、R02、R04、R05、R08、R09 的实现与反例覆盖完成，P1 至 P3 核心证明及其依赖可审阅，且严格注明参考实现尚未机械精化。M2 增加 R03、R06、R07、R14 和故障恢复证据。M3 增加 R10 至 R13、R16 及组合审计。

只要在声称假设内出现一个未授权效果、重复业务效果或错误释放反例，该保证即不得发布。修复需同时更新模型、实现和回归用例；不通过删除任务或更换统计口径消除失败记录。

## 24 实施拆分与交付清单

### 24.1 仓库结构

```text
avcl/
  spec/           核心语义 需求映射 schema
  core/           不可变类型 纯谓词 参考解释器
  compiler/       IR 校验 VC 监控器与门禁生成
  sdk/python/     受限构造器 与运行时客户端
  runtime/        prepare dispatch settlement
  ledger/         数据模式 迁移 事务实现
  adapters/       接口 注册 契约与实现
  supervisor/     核对 义务 时间与恢复
  formal/lean/    单条机械化证明主线
  models/tla/     有限并发与故障模型
  simulator/      独立真实状态 oracle
  tests/          语义 并发 故障 部署一致性
  examples/       退款场景与规范正反例
  evidence/       构建清单 证明与测试报告
```

该目录是建议实施布局，不表示本轮已经生成或实现这些组件。一个研究对象保持一个主定义：核心语义以 spec 和 Lean 对应定义为准，执行规范以 IR 为准，真实操作事实以持久账本与观测为准；生成物禁止手工修改后继续沿用旧摘要。

### 24.2 里程碑

|阶段与时间假设|主要工作|可验收交付|
|第 1 至 2 周|冻结 R01 至 R16、状态机、费用和标识语义|规范模式、反例目录、适配器契约草案|
|第 3 至 5 周|核心类型、IR、纯解释器、基础 VC|可执行参考语义、正反例一致性报告|
|第 6 至 8 周|账本、prepare、dispatch、模拟退款|M1 端到端闭环、P1 至 P3 证明构建|
|第 9 至 12 周|epoch、条件执行、未知核对、崩溃恢复|M2 故障矩阵与有限模型报告|
|第 13 至 16 周|稳定接口、适配器沙箱、精化缺口审阅|M2 集成验收、部署边界和风险清单|
|后续独立排期|委派、时序与信息流组合、配对实验|M3 证据包及 DSL 保留决策|

排期沿用两至三名熟悉编译器和形式方法成员的原型假设，不把 M3 全部能力挤入前十六周。职责建议为语义与证明、执行器与账本、适配器与独立测试三条工作线；人员不足时按依赖串行推进，不降低发布门槛。

### 24.3 交付完成定义

每个里程碑交付可运行代码、固定依赖、可复现命令、需求追踪、已知限制和失败记录。正式证明必须有内核检查构建，有限模型必须有配置及状态统计，测试必须有真实 oracle 轨迹。仅有架构图、示例语法或一份“通过”日志不能作为完成。

实施前优先确认三个业务参数：退款申请和重复业务动作的权威定义，费用及核对费用的上界，人工处置责任与时限。技术方案已为这些值提供配置位置，具体机构接口和业务 SLA 在接入时冻结。

## 附录 A 表层语言文法草案

以下是可选文本前端的最小 EBNF，定义模块、工具、合约和有限工作流结构。它是设计草案；首个实现可直接采用结构化 IR。表达式优先级依次为 or、and、not、比较、加减、常数乘法、字段访问；比较不允许链式写法。

```text
module      = "module", ID, "version", STRING, "{", {decl}, "}" ;
decl        = typeDecl | toolDecl | contractDecl | workflow ;
typeDecl    = "type", ID, "=", type, ";" ;
type        = "Bool" | "Int", "[", INT, ",", INT, "]"
            | "Id" | ID | "Option", "<", type, ">" ;
toolDecl    = "tool", ID, "(", ID, ":", type, ")", "->", type,
              "adapter", STRING, "effects", "[", ids, "]", ";" ;
contractDecl = "contract", ID, "{", {clause}, "}" ;
clause      = "requires", expr, ";"
            | "invariant", expr, ";"
            | "forbid", event, "when", expr, ";"
            | "before", event, "require", expr, ";"
            | "on", event, "owe", event, "within", DURATION, ";"
            | "success", expr, ";"
            | "assume", expr, "from", ID, ";" ;
```

工作流与表达式沿用同一词法层，定义如下。

```text
workflow    = "workflow", ID, "(", [params], ")", block ;
block       = "{", {stmt}, "}" ;
stmt        = "let", ID, "=", command, ";"
            | "if", expr, block, "else", block
            | "close", ID, "(", [args], ")", ";" ;
command     = "propose", ID, "using", ID
            | "validate", ID, "with", ID
            | "prepare", ID, "using", ID, "evidence", "[", ids, "]"
            | "dispatch", ID | "reconcile", ID
            | "delegate", ID, "to", ID, "scope", ID ;
expr        = orExpr ;
orExpr      = andExpr, {"or", andExpr} ;
andExpr     = notExpr, {"and", notExpr} ;
notExpr     = "not", notExpr | comparison ;
comparison  = sum, [cmpOp, sum] ;
sum         = term, {("+" | "-"), term} ;
term        = atom | INT, "*", atom ;
atom        = literal | ID, {".", ID} | "(", expr, ")" ;
cmpOp       = "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" ;
event       = ID, "(", [args], ")" ;
params      = ID, ":", type, {",", ID, ":", type} ;
args        = expr, {",", expr} ;
ids         = [ID, {",", ID}] ;
```

ID、INT、STRING、DURATION 和 literal 是词法记号；literal 首版仅含布尔、整数字面量、字符串和枚举常量。Id 必须按 schema 进行领域校验。INT 为十进制整数字面量，不接受指数或浮点；DURATION 为正整数加 ms、s、m 或 h。UTF-8 文件仅将 ASCII 标识符用于符号名，中文可用于注释和展示描述；字符串转义遵循明确的 JSON 字符串规则。

有界集合通过已声明的记录字段或 IR 常量表示，首版文本文法不提供集合推导与循环。合约条款的 requirement_id 及证据依赖采用元数据层绑定，不从注释猜测。附录的单一 owe 事件可通过命名派生事件表达“核对或已接收转交”，派生事件定义在 IR 中显式列出来源。

## 附录 B 事件封套与数据校验要点

```json
{
  "schema_version": "avcl.event/0.1",
  "event_id": "evt-1042",
  "tenant_id": "tenant-demo",
  "tenant_sequence": 1042,
  "operation_id": "op-42",
  "operation_revision": 3,
  "event_kind": "DispatchAdmitted",
  "actor": "runtime-gateway",
  "policy_digest": "sha256:<64 hex characters>",
  "request_digest": "sha256:<64 hex characters>",
  "authority_epoch": 12,
  "requirement_ids": ["R02", "R04", "R08"],
  "evidence_refs": ["ev-order-v7", "ev-approval-42"],
  "causal_parents": ["evt-1041"],
  "budget_deltas": [],
  "time": {"utc": "2026-10-25T08:00:00Z", "clock_id": "clock-a"}
}
```

上例摘要是占位符，实际 schema 应要求算法前缀与 64 位十六进制值。事件类型限定可枚举集合，additionalProperties=false；金额字段为规范非负十进制字符串。event_kind 决定 payload 的判别联合结构，派发事件不伪造远端回执字段。

API 可接受的 OperationRef 与审计事件完整内容分开。读取者按租户和对象权限获取受限投影，不能通过可猜测的 sequence 枚举其他租户信息。审计重放检查事件连续性、引用完整性、状态转移合法性和预算增量，但不宣称重放能发现所有未被观察的外部效果。

## 附录 C 设计决策与未决风险

|决策|默认选择|何时需要重新评审|
|语言形态|IR 加 Python SDK 优先|维护实验支持独立语法收益时|
|形式化主线|Lean 核心，TLA+ 有限模型辅助|团队无法维护或精化成本失控时|
|一致性边界|租户共享事务锁和强一致账本|多地域或租户级性能瓶颈时|
|预算治理|调用前上界，多账户原子预留|外部计费无法给上界时降级|
|未知效果|保留预留，持续核对或转交|远端协议或业务处置规则变化时|
|撤销保证|本地派发准入时点|业务要求远端生效瞬间授权时|
|多 Agent|共享根账户及子限额|离线托管或跨账本分布式消费时|
|部分效果|显式记录，严格退款按违约处置|接入天然多阶段工具时|

主要风险是远端协议不足、费用上界不可靠、规范漏译、形式模型与实现脱节以及长时间 Unknown 占用预算。对应措施分别是接入门槛、明确软硬保证、需求正反例审阅、精化和独立故障测试、持久监督及容量背压。它们需要落实为证据，不以增加语法关键词替代。

## 附录 D 设计依据与参考资料

[S0] Agent形式化验证语言研究与设计.docx。附件标题为《面向 LLM Agent 的形式化验证语言设计》，AVCL 研究与设计方案第一版，资料核查日期 2026 年 10 月 25 日。本文需求追踪按该附件章节定位。源文件 SHA-256 为 0543471efc21c36a0cbfcf210a1c0fe9be9c111bb894152d32cac00722b8e00a。

[S1] RFC 8785 JSON Canonicalization Scheme，2020。用于确定性 JSON 表示的实现参考；其并不自动完成业务语义规范化。https://www.rfc-editor.org/rfc/rfc8785.html

[S2] PostgreSQL 18 Documentation，13.2 Transaction Isolation。用于事务隔离和序列化失败的重试约束。https://www.postgresql.org/docs/18/transaction-iso.html

[S3] RFC 9110 HTTP Semantics，2022，第 9.2.2 节与第 13.1.1 节。用于区分 HTTP 幂等语义与条件请求；业务退款的严格幂等仍须具体服务协议。https://www.rfc-editor.org/rfc/rfc9110.html

[S4] AWS Builders Library，Making retries safe with idempotent APIs。用于请求身份、重试和服务端协议设计参考。https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/

[S5] The Lean Language Reference，Elaboration and Compilation。用于区分证明项的内核检查与外部实现验证。https://lean-lang.org/doc/reference/latest/Elaboration-and-Compilation/

S1 至 S5 的官方页面在本轮设计中核对。研究新颖性与近邻比较沿用总体方案的定位，本文不重复宣称完成新的文献系统综述，也不将原附件的实验数字视为本轮复现。
