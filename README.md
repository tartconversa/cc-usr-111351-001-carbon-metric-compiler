# 园区碳指标的维度语义编译与情景重算

计量资料载入模块 + 指标计算内核。读取车间活动、覆盖关系和口径定义，保留原始数值及资料版本；内核在读取结果之上提供多个核算口径共用的声明式指标计算。

## 资料读取（src/carbon_metric_compiler_input.py）

提供 JSON 资料结构读取与字段存在性检查，保留原始数据。
fixtures/sample.json 使用合成记录；读取时不执行推断、匹配、规划或核算。
schema_version 当前为整数 1，主集合为 records；其余字段保留在返回的字典中。
此版本没有第三方运行依赖。

    python -m src.carbon_metric_compiler_input fixtures/sample.json

## 指标计算内核（src/carbon_metric_compiler_kernel.py）

分析人员用受限的声明式表达式定义用量、排放、强度和情景差值；定义可以引用其他指标、选择维度和期间，但不能执行任意 Python。

### 资料模型

- records：计量记录，带稳定 observation_id、计量边界 boundary、期间 period 和若干计量字段；同一边界/期间/计量只允许一条记录（重复即拒绝，防止双计）。
- coverage：总分表覆盖关系 `{parent, children, measures?}`。measures 缺省时按资料推导（总表边界出现过的计量才适用该组），产量等非表计计量不会被卷入总分表裁决。
- units / measures：计量登记。measures 可声明 `kind: "flow" | "stock"`（流量/存量）；units 中的计量默认为流量。
- factors：因子带适用活动 activity、地区 region、有效区间 valid_from/valid_to、版本 version、优先级 priority。同一活动的因子单位必须一致。
- definitions：指标定义 `{name, expression}`，表达式为字符串。

### 表达式语言

    measure("electricity", boundary=["workshop-a"], period=["2026-10"])
    sum(<expr>, over=["boundary"])          mean(<expr>, over=["period"])
    where(<expr>, boundary=[...], period=[...])
    apply_factor(<expr>, activity="electricity", region="default")
    under(<expr>, "scenario-name")
    <指标名>                                引用其他定义
    <expr> + - * / <expr>                   四则运算与数字字面量

表达式经 ast 白名单解析为受限节点，属性访问、导入、推导式、条件表达式等构造一律拒绝。

### 执行前静态检查

每次查询先对全部指标做静态分析再执行：

- 依赖环：指标引用成环即拒绝并给出环路径；
- 量纲：加减要求量纲一致，乘除做量纲代数；
- 粒度：二元运算要求粒度一致（标量可广播），聚合维度必须在粒度中；
- 可加维度：sum/mean 只接受简单量纲——强度等复合量纲必须先分别汇总分子分母再相除，从语言层面禁止"先算比率再平均"；存量不能按期间求和（同单位不代表可相加），存量/流量不能相加减。

### 覆盖裁决（口径）

一次查询选定一个口径 caliber，全查询共享同一裁决：

- `parent`（总表）：覆盖组内总表记录为准，分表记录排除并逐条列入 issues；总表缺某一期间时分表不回填，该坐标以缺值单元保留并记 basis-unavailable。
- `children`（分表）：分表记录为准，总表记录排除；某块分表缺记录时保留不完整范围，记 incomplete-children（列出缺的边界与期间），不静默用总表补齐。

裁决在 measure 源节点完成，因此任何上游聚合都不可能同时拿到总表与分表：厂区总表 100 与分表 40、60 在两种口径下都得 100，结构上不存在得到 200 的路径。

### 值的状态

每个坐标单元三态分离：value（含有效零，即真实计量到的 0）、missing（无支撑记录）、zero_denominator（分母为零）。汇总强度一律由分子分母重算：10/2 与 90/30 的整体强度是 100/32=3.125，而不是 (5+3)/2=4。where 筛选在求值时下推到 measure 源节点，比率由筛选后的原子记录重算，而不是在算完的比率上删行。

### 因子解析与情景

- 因子按（活动, 地区, 期间）在查询固定的因子集内解析；同优先级重叠抛出 FactorConflictError 并列出全部候选，不按文件顺序碰运气；优先级不同则高者优先；某期间无适用因子时该坐标为 missing 并记 no-factor。
- 查询接口固定资料版本（data_version）与因子集：基线 `factors={"versions": [...]}`，情景 `scenarios={"名称": {...}}`。
- `query.compare(metrics, scenarios)` 批量情景比较：逐坐标给出基线/情景值、差值、支撑的原子记录与因子。
- `query.impact(factor_id, metrics)` 因子影响：更换一项因子时，哪些结果坐标由它支持、对应哪些原子记录。
- 定义内可用 `under(expr, "情景名")` 写情景差值指标，如 `under(emission, "new") - emission`。

### 缓存与复用

缓存身份包含口径、输入、因子和筛选语义：

- measure 节点键 = 表达式 + 筛选上下文 + 口径 + 数据盐（记录/覆盖/计量的内容哈希）；
- 因子节点键 = 活动/地区 + 情景因子集盐 + 依赖内容键；
- 中间节点键 = 运算 + 依赖内容键。

更换一项因子时，计量源子图完全复用，未受影响的下游经内容键命中缓存；失败的分支（如因子冲突）只抛异常，不写入缓存，不会留下像成功结果的缓存项。

### 运行

    python -m src.carbon_metric_compiler_kernel fixtures/kernel_sample.json \
        --metric park_intensity --caliber children \
        --factor-versions v2026.10 --scenario new_factor=v2026.11 \
        --data-version demo-2026-11

## 检查

运行全部检查（资料读取 + 内核）：

    python -m unittest discover -s tests -v

检查源码可编译性：

    python -m compileall -q src tests
