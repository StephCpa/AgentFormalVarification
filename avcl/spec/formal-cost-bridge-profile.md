# P4 现实成本桥接：条件模型与实现验收

状态：2026-10-26，抽象条件定理和有限反例检查已实现。对应详细设计 P4、R05、R14、T05、T14；不表示真实适配器已获证明，也不改变详细设计 v0.1 的原始文件。

## 设计目的与量的区分

P3 的 `spent + pending ≤ limit` 只约束本地账本。P4 还需证明现实已经发生的累计成本 `actual ≤ spent + pending`。例如退款已发生而回执丢失时，`actual=2, spent=0, pending=3` 合法；若超时即释放 pending，本地账本仍不超预算，但现实成本失去覆盖。

每个逻辑操作记录如下；金额均为同一计价单位的自然数，不涉及汇率换算。

|字段|语义与责任方|
|---|---|
|id|逻辑操作唯一标识；本地记录不可重用|
|upper|操作全部本金、手续费和允许重试费用的冻结上界|
|actual|独立远端产生的累计实际成本；不是从本地成功状态计算|
|spent|本地已确认并累计入账的成本|
|remaining|为尚未终结成本保留的承诺|
|remoteClosed|所有绑定成本来源均不能再产生新增费用的远端事实|
|phase|prepared、admitted、unknown、partialCost、final、aborted|

`actual` 与 `remoteClosed` 是模型中的环境事实，生产实现不能自行置真。业务成功、收费回执中的单个 final 标记、查询无记录和超时，均不能独立替代全部成本来源的终局保证。

## 转移与守卫

|转移|必要前提|本地账本或远端事实变化|
|---|---|---|
|Prepare|新鲜 id；所有现有承诺加 upper 不超过 limit|spent=0，remaining=upper，actual=0|
|Admit|prepared|进入 admitted，不释放承诺|
|Timeout|admitted|进入 unknown，不释放承诺|
|RemoteCharge|已准入；remoteClosed=false；新累计成本单调且 ≤ upper|仅更新 actual|
|RemoteClose|已准入|关闭全部未来成本来源；不自动结算本地账本|
|ObservePartial|活动操作；旧 spent ≤ reported ≤ actual|spent=reported，remaining=upper−reported|
|ObserveFinal|活动操作；reported=actual；remoteClosed=true|spent=reported，remaining=0|
|AbortPrepared|尚未准入|spent=actual=remaining=0|
|Retain|不产生新的逻辑效果或费用|幂等重试、重复回执等保持当前状态|

部分观察允许落后于现实成本，但必须单调且不虚构成本。重复观察不能再次累计收费；累积数值不是“本次增量”。收费重试必须作为 RemoteCharge 计入同一 upper；Retain 仅适用于没有新增收费的重试。

最终 phase 仍属于“曾准入”，所以阻止终局后新费用的关键是 `remoteClosed=false` 守卫及其真实语义，不能仅依赖本地状态名称。准备后尚未准入的取消不依赖远端回执；准入后的取消必须取得权威终局才能释放。

## 形式化结果

文件：[`CostBridge.lean`](../formal/lean/CostBridge.lean)，Lean 4.33.0，仅导入 Std。

- `step_valid`：单次正常转移保持费用上界、观察不超真实费用及各阶段形状约束。
- `P4_cost_bridge`：任意有限合法账本中，逐操作真实成本之和不超过逐操作本地承诺之和。
- `P4_actual_within_budget`：初态合法、操作 id 唯一、初始承诺满足预算，且执行仅经过模型允许的有限转移时，末态满足 `Σactual ≤ Σ(spent + remaining) ≤ limit`，并保持 id 唯一。
- `example_partial_then_final`：非空执行包含准备、准入、实际本金 2、丢回执、部分确认 2、迟到费用 1、远端关闭及最终结算 3。
- `example_local_budget_insufficient`：构造本地承诺为 0、实际成本为 2 的状态，证明本地预算不超额本身不足以推出 P4。
- `example_closed_blocks_charge`：构造终局状态与新增计费守卫不相容的实例。

三项一般定理依赖 `propext, Quot.sound`，非空执行实例依赖 `propext`，后两项具体实例无公理依赖。无自定义公理或 sorry。`AdapterSoundness`、`PartialReceiptSoundness`、`ReceiptSoundness` 是每次转移显式提交的前提，不是被隐藏的外部事实证明。验证脚本逐项核对真实公理依赖声明。

该模型直接组合单账户预算与实际成本桥接，不是对 `Core.lean` 或 `Delegation.lean` 的机械导入精化。一般有限执行的归纳证明不限一个操作，但限额在执行中固定；政策更新、跨币种和多维成本映射须单独建立关系。

## 真实适配器必须履行的契约

|义务|上线前所需证据|当前状态|
|---|---|---|
|全成本上界|本金、请求费、执行费、查询费、重试费的完整封顶契约；无法封顶则不得声明硬预算|固定退款原型有上界，通用收费工具未完成|
|成本归属完整|每次真实收费映射到唯一逻辑操作；无旁路、无遗漏；根/子账户视图不能重复相加|模型内 id 唯一；真实出口与生产适配器未验证|
|远端防重|重发不产生第二次业务效果；收费重试仍计入 upper；明确去重保留期限|默认模拟器永久防重；可选有限保留与双端截止见 [发送窗口规格](send-window-profile.md)；真实服务契约未验证|
|观察真实性|来源认证、operation/request 摘要绑定、累计值单调，冲突不可覆盖历史|受信入口和绑定检查已实现；生产来源认证未完成|
|终局完整性|全部效果和费用已终结，当前 reported 完整；不存在未决收费重试或后续费用|原型依赖适配器观察；真实终局契约未验证|
|实现精化|实际状态、原子预留和结算能够模拟抽象转移|定向测试覆盖代表路径；Python/SQL 的机械精化未完成|

实际费用超 upper 或终局后新增费用违反正常定理前提。系统应记录真实费用、保留争议与隔离状态、阻止新增支出；不能为了继续满足账本不等式而截断实际费用。本规格不保证事故发生到观察到达之间的现实成本仍被覆盖。

## 有限模型与运行测试

[`cost_bridge_model.py`](../models/cost_bridge_model.py) 独立枚举单操作、本金 2、手续费最多 1、预算 3。正确模型有 41 个状态、135 条转移，包括幂等自环；无违反桥接或预算的状态。该数量独立于既有双操作预算模型和委派模型，不能相加作为系统状态覆盖。

四种故意错误变体均生成可重放反例：

|变体|反例说明|
|---|---|
|early_timeout_release|超时清空预留后，迟到执行产生真实本金|
|final_on_partial|把不完整观察当作终局，之后仍发生本金或费用|
|underestimated_upper|只预留本金 2，真实本金加费用达到 3|
|post_final_cost|终局释放后，远端仍允许产生新增费用|

模型未包含多操作并发、崩溃、回执乱序、主体身份或数据库精化。其正常费用重试不新增成本；收费重试仅被费用总上界抽象，不表示现实次数或保留期限已经验证。

[`test_cost_bridge.py`](../tests/test_cost_bridge.py) 另以独立 RemoteMeter 驱动参考内核，先发生本金、丢弃观察，再部分结算，之后才新增手续费；检查每一关键步骤的租户与订单覆盖，并验证幂等重放、费用型无效果拒绝、违约成本入账和新增支出隔离。RemoteMeter 仅为测试环境，不读取内核账本来推算现实成本，也不替代真实服务。

## 验收与剩余工作

运行 `verify.cmd`，检查新阶段 `cost-bridge-model`、`lean-cost-bridge` 及单元测试；每次报告冻结模型 JSON、编译日志、公理门禁和源文件摘要。以对应 `evidence/<UTC>/verification.json` 的结果为准。未指定 `--postgres` 时不得宣称本轮数据库集成测试通过。

下一步是为真实工具定义并验证上界、去重窗口和终局契约，建立生产来源认证及出口全中介，再连接多账户/委派模型与实现精化。P4 抽象条件证明完成，不等于整个系统或真实工具的无条件费用安全证明完成。
