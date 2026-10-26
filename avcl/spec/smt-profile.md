# 纯谓词 SMT 验证后端 v0.1

对应详细设计 §7.3、§8、R09/R13。输入是既有 `avcl/predicate/0.2`，不是另一套手写退款模型。公共入口重新加载、类型检查 IR，重新生成 `avcl/checks/0.2` 指令，然后分别编码树语义与实际生成指令的控制流。运行门禁、账本、工作流与候选包加载器均不调用求解器。

## 查询与状态

设 D 为完整声明输入域，A/V 为 AST/栈式检查结果，DA/DV 为对应路径上有定义。

|性质|查询|可支持的结论|
|---|---|---|
|input-domain-inhabited|D|Sat 模型经类型归一化后为 ProvedUnder；Unsat 为 AssumptionBroken|
|checks-equivalent|D ∧ ¬(DA ∧ DV ∧ A=V)|已确认域非空且 Unsat 为 ProvedUnder；Sat 必须在两个实际执行器回放，确有差异或求值错误才为 Violated|
|allowed-action-exists|D ∧ DA ∧ A|已确认域非空且 Sat 见证在两个执行器均允许，为 ProvedUnder；Unsat 为 Violated|

`ProvedUnder` 仅涉及该 IR、该输入域及本表性质，并依赖报告列出的编码器、类型检查器、Python 和固定 Z3 版本。Sat/Unsat 与性质状态分别记录；求解器 Unknown、Timeout、Error 均不建立证明。模型不能解码、类型校验失败、反例无法回放时保留原求解器状态，性质为 Unknown。前置输入域未确认时，后续查询不能以 Unsat 获得空洞证明。全部拒绝的政策即使一致性成立，也无法通过允许动作存在性检查。

本纯谓词后端不编码操作状态转移、时序触发器、任务完成可达性、工作流副作用或数据库语义。另有 [合约联合 SMT 后端](contract-smt-profile.md) 检查共享输入、条款冲突及假设声明有效期，并给出可回放的不可再删减冲突集；一般假设表达式仍未实现。这些局部查询不能解释为完整 §8 验收。

## 值与执行编码

- Bool 为逻辑布尔，Int 为数学整数；输入上下界来自 IR，运算不使用位宽截断，不把派生表达式重新限制在 18 位。
- Enum 为成员索引，名义名称与成员表由既有类型检查器固定。
- String/Id 为整数序列，长度最多 256，Id 非空；每个字符限于 U+0000—U+10FFFF 并排除代理码点。使用 Seq(Int) 避免将 Python 的 Unicode 域缩窄到求解器原生 String 字母表。
- Record/Option 为代数数据类型；记录字段顺序规范化。Option 解包的定义条件按实际 And/Or/If 短路路径传播。
- Set 为长度不超过声明 max_size 的元素序列，元素必须满足递归类型约束。序列允许不同顺序和重复代表；所有集合相等/成员关系都递归采用外延语义，模型回放归一化为 frozenset。每个合法有限集合都有序列代表，重复代表不增加集合的真实基数。集合约束按各操作数的声明容量展开，避免嵌套的成员量词；条件分支按合并后的容量处理。嵌套集合、包含集合的 Record/Option 同样适用。
- AST 递归生成表达式及定义条件；字节码后端独立跟踪程序计数器、栈和路径条件，执行真实 PUSH/LOAD/GET、算术、比较和前向跳转。终点要求单个 Bool，无法覆盖的路径不被当成正常返回。公共入口不接受外部字节码；内部差分测试入口只用于编译器变异反例。

支持现有全部纯值类型和 16 个表达式构造，不承诺每个合法输入都能在有限资源内求解。量词、嵌套集合及大量分支可能达到上限。默认每个查询 5000 ms、Z3 rlimit 1000000、编码步骤 50000、模型解码 10000 个节点；上限在报告内冻结，超限为 Unknown，不退化为抽样后宣称证明。Z3 超时是求解器资源控制，不是整个 Python 进程的强隔离或墙钟终止保证。

## 工具与证据

固定 `z3-solver==4.15.4.0` / Z3 core `4.15.4`。`requirements-smt.txt` 锁定发行 wheel 的 SHA-256；安装来源为 [PyPI 对应发行版](https://pypi.org/project/z3-solver/4.15.4.0/)。求解、未知原因和 SMT-LIB 导出使用 [Z3 官方 Solver API](https://z3prover.github.io/api/html/classz3py_1_1_solver.html)。没有独立证书检查器，Z3 和编码器属于可信计算基；两个编码共享值/基本运算实现，不能用二者一致证明共同部分无误。

每个报告绑定 requirement_id、IR/来源/验证规格摘要、编译器与编码器版本、checks SHA-256、工具版本、显式假设、完整字段域、资源限制、查询 SHA-256、时间、见证引用和回放结果。`spec_digest` 绑定代码中的机器可读查询/语义规格，不冒充原设计 DOCX 的摘要。`witness_ref` 是指向当前报告 bundle 的 JSON Pointer。`runtime_obligations=[]` 表示这些离线纯谓词查询不创建运行义务，不表示工作流没有义务。

证据目录同时保存规范 IR、生成 checks、完整 JSON 报告、每个查询的 SMT-LIB 与逐文件摘要。报告中的原始模型、求解状态、统计和回放可审阅。导出目录必须不存在，不能覆盖历史记录；unsigned-offline-evidence 不属于可激活政策包。`scripts/verify.py --smt` 额外运行真实求解测试与示例并冻结全仓源码摘要；默认验证明确标注未测试 SMT，缺失依赖不会跳过后报告通过。

```powershell
# 在 avcl 目录；现有 .venv 由项目环境提供
uv pip install --python .venv/Scripts/python.exe --require-hashes -r requirements-smt.txt
.\verify.cmd --smt
.\.venv\Scripts\python.exe scripts/verify_smt.py path/to/core.ir.json path/to/new-evidence
```

单文件命令退出码：0 表示本表三项性质均有条件成立；1 表示至少一项 Violated/AssumptionBroken；2 表示未发现反驳但存在 Unknown。具体性质必须读取报告，不能以退出码替代性质、求解器和操作的三套状态。

验证包括实际退款大金额域、原 120 输入结构化示例、Unicode 高码点、空 Id/非法代理码点、嵌套 Option、集合外延相等、数学整数、全部拒绝与资源不足。小域逐输入对照真实 AST/VM 的值和定义性；比较、算术、短路编译器变异要求产生可回放反例；导出的查询在新求解器中重载。注入的 Timeout/Error 用于分级逻辑测试，与实际资源上限查询分开；都不声称生产故障测试。

## 运行资源条件补充

运行解释器现受[工作量预算](evaluation-budget-profile.md)限制。SMT 查询仍表达理想纯逻辑，未编码燃料；报告列出 evaluation-work-sufficient 假设、runtime_budget_encoded=false 和运行时预算义务。Unsat 等价结论不能扩展成任意资源配置下的实现等价。Sat 实际回放耗尽时属性为 Unknown，既不签发允许见证，也不当作逻辑反例；输入域非空只说明类型域中存在值。
