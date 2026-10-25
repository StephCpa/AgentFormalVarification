# 纯谓词 IR v0.2 实现规格

实现依据为详细设计第 4、6、7、8 章。本规格描述单谓词编译链。其上的合约 v0.3 提供条款与发布门禁，工作流 v0.4 提供有限命令的仿射/效果检查和执行，分别见 `contract-policy.md`、`workflow-profile.md`。完整工作流命令、监控器、生产发布密钥和 VC 编码尚未完成。

## 数据与版本

`ir_version = avcl/predicate/0.2`，`semantic_version = avcl/pure/0.2`，生成器为 `avcl/checks/0.2`。未知版本、未知键、重复 JSON 键、JSON 数字和非法 Unicode 均拒绝。最大编码 64 KiB；金额与所有 IR 整数均使用规范十进制字符串，IR 逻辑常量允许负数，禁止 `-0` 和前导零。

文档字段固定为 `ir_version, semantic_version, requirement_id, fields, root, source_map`。节点以 `n0`、`n0.0` 等树位置形成确定性 ID；相同结构的节点定位稳定，编辑树结构可以改变 ID，不将它当作跨版本永久身份。来源映射值是显式源码定位字符串。诊断提供 `code, requirement_id, node_id`。

`semantic_digest` 覆盖字段类型、表达式、需求编号与语义版本；`source_digest` 单独覆盖来源映射；`artifact_digest` 覆盖整个 IR 文件。来源定位变化不影响运行语义摘要。Int 在输入业务类型中限定到正负 18 位整数范围，算术采用数学整数，结果区间由静态区间传播推导，可以超出输入范围而不发生机器回绕。If 合并两个分支的区间/规模范围。

## 类型和值

|类型|宿主表示|JSON 值表示/检查|
|---|---|---|
|Bool|`bool`|JSON 布尔；与整数严格区分|
|Int|`int`|规范十进制字符串，输入必须在声明上下界内|
|String|`str`|严格 Unicode 字符串，最多 256 字符|
|Id|`Tagged(type_name, value)`|字符串；按名义类型区分主体 ID、订单 ID 等|
|Enum|`Tagged(type_name, member)`|仅接受声明的有限成员；同名不同定义拒绝|
|Record|dict 或不可变 Record|精确字段集；多余/缺失字段拒绝；进入求值前递归冻结|
|Option|None 或 `Some(value)`|`{"tag":"None"}` 或 `{"tag":"Some","value":...}`；可嵌套|
|Set|`frozenset`|按元素编码排序的数组；元素类型固定，最多 64 个，重复编码拒绝|

类型树深度最多 16，单类型展开最多 1000 节点，记录字段最多 64。输入和常量都按类型递归归一化为不可变值；调用方随后修改原 dict 不改变已编译常量或当前输入快照。

## 表达式与总定义性

支持 Const、Field、Get、Eq、Lt、Le、Add、Sub、MulConst、Not、And、Or、If、InSet、IsSome、Unwrap。禁止任意调用、eval、变量乘变量、除法、循环和未知构造。MulConst 的左侧必须是 Int 常量。谓词结果必须为 Bool。

AST 最多 1000 节点、深度 64。所有分支先静态检查，短路不可隐藏非法字段、工具调用或类型错误。And/Or 左到右短路，If 只执行选中的分支。Unwrap 需要当前路径已知同一纯表达式为 Some：And 右侧使用左侧为真的事实，Or 右侧使用左侧为假的事实，If 两侧分别使用条件真/假的事实。嵌套 Option 每一层都需要独立守卫。

例如 `IsSome(x) and Unwrap(x) > 0` 有定义；`IsSome(x) or Unwrap(x) > 0` 拒绝，因为右侧运行时 x 反而为空。事实按完整类型化表达式匹配，不会把 x 的守卫用于 y。输入冻结且表达式无副作用，重复出现的同一表达式不会在两次求值之间变化。

## 生成门禁与包加载

树解释器直接读取 IR。栈式门禁只支持 PUSH/LOAD/GET、比较算术、集合成员、Option 操作和前向条件跳转。没有动态导入、Python eval 或外部调用。检查器与解释器共用输入类型规范化，执行逻辑分别实现；一致性证据不覆盖共用类型检查器自身的正确性。

候选包包含 `core.ir.json, checks.json, source-map.json, verification-report.json, manifest.json`。manifest 固定文件集合与摘要、生成器版本、语义/来源摘要，状态明确为 `unsigned-candidate`。加载时使用宿主提供的可信 IR 摘要，检查全部文件、重新类型检查 IR、重新生成并比较检查产物。重新计算一个被篡改 checks 文件的哈希不能绕过重生成比较。报告中的证据状态不授予动作权限。

退款内核的 prepare 和 dispatch 使用生成门禁；Operation、许可摘要、Prepared 和 DispatchAdmitted 事件绑定 IR 与合约摘要。政策签名/切换由合约 v0.3 的内存宿主域管理，旧操作仍持有其原始快照，持久发布与恢复尚未实现。

## 分项验证

离线枚举对 Bool、小范围 Int、Enum、Record、Option、有界 Set 的声明域计算完整笛卡尔积，默认最多 10000 实例。String/Id 或过大的域不抽样冒充完整验证，结果为 Unknown。报告包含 property_id、requirement_ids、IR/来源摘要、checker_build、假设、域与数量、时间、见证。

`checks-equivalent` 比较树解释器与生成门禁；`allowed-action-exists` 检查至少一个允许输入，并保留允许/拒绝见证。成功仅为 BoundedChecked；全部拒绝时后者为 Violated。这里没有 SMT 求解，`solver_status` 为 null，也不产生 ProvedUnder。运行检查另给 RuntimeSatisfied/Violated/Unknown；错误或未知均不允许当前输入。

`examples/ir_pipeline.py` 的结构化退款示例覆盖 120 个完整输入：金额 0—3、两种币种、None/0—3 的批准额、最多一种允许币种。该域不是生产业务边界；实际退款谓词仍按真实字段范围生成，其离线枚举报告为 Unknown。两份包并列保留，不能只展示有界示例的通过报告。
