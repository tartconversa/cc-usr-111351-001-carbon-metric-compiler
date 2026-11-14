"""指标计算内核：供多个核算口径共用的声明式指标计算。

在资料读取模块（carbon_metric_compiler_input）之上提供：

- 受限声明式表达式：白名单函数加四则运算，不能执行任意 Python；
- 执行前静态检查：依赖环、量纲、粒度、可加性（存量/流量、复合量纲）；
- 覆盖裁决：总表与其覆盖的分表只取一种计量依据，分表缺块时保留不完整范围；
- 强度由分子分母重算；缺值、有效零、零分母分别表达；筛选下推到原子记录；
- 固定资料版本与因子集的查询接口、批量情景比较与因子影响追溯；
- 缓存身份包含口径、输入、因子与筛选语义；失败分支不写入缓存。

表达式语言（字符串形式，解析为受限 AST）：

    measure("electricity", boundary=["workshop-a"], period=["2026-10"])
    sum(<expr>, over=["boundary"])          mean(<expr>, over=["period"])
    where(<expr>, boundary=[...], period=[...])
    apply_factor(<expr>, activity="electricity", region="default")
    under(<expr>, "scenario-name")
    <指标名>                                引用其他定义
    <expr> + - * / <expr>                   四则运算与数字字面量

语义约束：

- sum/mean 只接受简单量纲（单单位或无量纲），强度等复合量纲必须先分别
  汇总分子分母再相除，从语言层面禁止“先算比率再平均”；
- sum 按期间聚合存量（kind="stock"）会被静态拒绝，同单位不代表可相加；
- where 的筛选在求值时下推到 measure 源节点，比率始终由筛选后的原子
  记录重算，而不是在算完的比率上删行；
- apply_factor 要求粒度含 period，按（活动, 地区, 期间）在查询固定的
  因子集内解析；同优先级重叠抛出 FactorConflictError，不按文件顺序碰运气。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from src.carbon_metric_compiler_input import load_bundle

DIMS = ("boundary", "period")
FLOW = "flow"
STOCK = "stock"
VALUE = "value"
MISSING = "missing"
ZERO_DENOM = "zero_denominator"
CALIBERS = ("parent", "children")
BASELINE = "baseline"

_BUILTINS = ("measure", "sum", "mean", "where", "apply_factor", "under")
_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_SELECTOR_DIMS = frozenset(DIMS)


class KernelError(Exception):
    """内核错误的基类。"""


class DataError(KernelError):
    """资料内容不满足内核要求。"""


class DefinitionError(KernelError):
    """指标定义在解析或静态检查阶段被拒绝。"""


class FactorConflictError(KernelError):
    """同优先级因子重叠，无法确定唯一因子。"""


# ---------------------------------------------------------------- 量纲

_UNIT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class Dim:
    """量纲：单位到指数的映射，如 kgCO2e/t。不可变、可哈希。"""

    __slots__ = ("powers",)

    def __init__(self, powers=()):
        self.powers = tuple(sorted(powers))

    @classmethod
    def parse(cls, text):
        if not isinstance(text, str) or not text.strip():
            raise DataError("单位必须为非空字符串：" + repr(text))
        powers = {}

        def add(token, exp):
            if token == "1":
                return
            if not token or any(ch not in _UNIT_CHARS for ch in token):
                raise DataError("无法识别的单位：" + text)
            powers[token] = powers.get(token, 0) + exp
            if powers[token] == 0:
                del powers[token]

        parts = text.split("/")
        for token in parts[0].split("*"):
            if token:
                add(token, 1)
        for den in parts[1:]:
            for token in den.split("*"):
                if token:
                    add(token, -1)
        return cls(powers.items())

    def mul(self, other):
        merged = dict(self.powers)
        for unit, exp in other.powers:
            merged[unit] = merged.get(unit, 0) + exp
            if merged[unit] == 0:
                del merged[unit]
        return Dim(merged.items())

    def div(self, other):
        return self.mul(Dim((unit, -exp) for unit, exp in other.powers))

    @property
    def is_simple(self):
        """简单量纲：无量纲或单单位一次方。复合量纲（如强度）不可直接聚合。"""
        return len(self.powers) <= 1 and all(exp == 1 for _, exp in self.powers)

    def __eq__(self, other):
        return isinstance(other, Dim) and self.powers == other.powers

    def __hash__(self):
        return hash(self.powers)

    def __str__(self):
        if not self.powers:
            return "1"
        num = [unit for unit, exp in self.powers if exp > 0]
        den = [unit for unit, exp in self.powers if exp < 0]
        text = "*".join(num) if num else "1"
        if den:
            text += "/" + "*".join(den)
        return text

    def __repr__(self):
        return "Dim(%s)" % str(self)


# ---------------------------------------------------------------- 签名与值

@dataclass(frozen=True)
class Sig:
    """表达式静态签名：量纲、存量/流量、粒度。"""
    dim: Dim
    kind: str
    grain: frozenset


class Cell:
    """一个坐标上的值。state 区分 有效值(含有效零)/缺值/零分母。"""

    __slots__ = ("state", "value", "obs", "facs")

    def __init__(self, state, value, obs, facs):
        self.state = state
        self.value = value
        self.obs = frozenset(obs)
        self.facs = frozenset(facs)


class Frame:
    """指标求值结果：签名 + 坐标到 Cell 的映射 + 结构问题列表。"""

    __slots__ = ("sig", "cells", "issues", "content_key")

    def __init__(self, sig, cells, issues):
        self.sig = sig
        self.cells = dict(cells)
        seen = set()
        unique = []
        for issue in issues:
            key = json.dumps(issue, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(issue)
        self.issues = tuple(sorted(unique, key=lambda i: json.dumps(i, ensure_ascii=False, sort_keys=True)))
        self.content_key = (
            sig,
            tuple(sorted(
                (coord, cell.state, cell.value, tuple(sorted(cell.obs)), tuple(sorted(cell.facs)))
                for coord, cell in self.cells.items()
            )),
            tuple(sorted(seen)),
        )


@dataclass(frozen=True)
class _Ctx:
    """求值上下文：筛选语义、情景、口径。进入缓存键。"""
    boundary: frozenset | None
    period: frozenset | None
    scenario: str | None
    caliber: str

    def cache_part(self):
        # 情景不直接进入键：因子节点已通过情景因子集盐值区分，
        # 其余节点与情景无关，从而跨情景共享缓存。
        return (self.boundary, self.period, self.caliber)


# ---------------------------------------------------------------- 表达式节点

@dataclass(frozen=True)
class Num:
    value: object


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Measure:
    measure: str
    selectors: frozenset  # frozenset((维度, frozenset(取值)))


@dataclass(frozen=True)
class Bin:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Agg:
    op: str
    expr: object
    over: tuple


@dataclass(frozen=True)
class Where:
    expr: object
    selectors: frozenset


@dataclass(frozen=True)
class ApplyFactor:
    expr: object
    activity: str
    region: str


@dataclass(frozen=True)
class Under:
    expr: object
    scenario: str


def _sel_get(selectors, dim):
    for name, values in selectors:
        if name == dim:
            return values
    return None


def _fail(text, node):
    raise DefinitionError("%s：%s" % (text, type(node).__name__))


def _const_str(node, what):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise DefinitionError(what + "必须为字符串字面量")


def _selector_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset([node.value])
    if isinstance(node, (ast.List, ast.Tuple)):
        return frozenset(_const_str(item, "筛选值") for item in node.elts)
    raise DefinitionError("筛选条件必须为字符串或字符串列表")


def _selector_keywords(call, allowed):
    selectors = []
    for kw in call.keywords:
        if kw.arg is None or kw.arg not in allowed:
            raise DefinitionError("不支持的筛选参数：" + repr(kw.arg))
        selectors.append((kw.arg, _selector_value(kw.value)))
    return frozenset(selectors)


def _convert(node):
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DefinitionError("表达式中只允许数字字面量")
        return Num(value)
    if isinstance(node, ast.Name):
        if node.id in _BUILTINS:
            raise DefinitionError("内置函数必须被调用：" + node.id)
        return Ref(node.id)
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise DefinitionError("不支持的运算符：" + type(node.op).__name__)
        return Bin(op, _convert(node.left), _convert(node.right))
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return _convert(node.operand)
        if isinstance(node.op, ast.USub):
            return Bin("*", Num(-1), _convert(node.operand))
        raise DefinitionError("不支持的运算符：" + type(node.op).__name__)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _BUILTINS:
            raise DefinitionError("只允许调用内置函数：" + ", ".join(_BUILTINS))
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise DefinitionError("不支持星号参数")
        name = node.func.id
        if name == "measure":
            if len(node.args) != 1:
                raise DefinitionError("measure 需要一个计量名称参数")
            return Measure(_const_str(node.args[0], "计量名称"),
                           _selector_keywords(node, _SELECTOR_DIMS))
        if name in ("sum", "mean"):
            if len(node.args) != 1:
                raise DefinitionError(name + " 需要一个表达式参数")
            over = None
            for kw in node.keywords:
                if kw.arg != "over":
                    raise DefinitionError(name + " 只接受 over= 参数")
                over = _selector_value(kw.value)
            if not over:
                raise DefinitionError(name + " 必须通过 over=[...] 指定聚合维度")
            return Agg(name, _convert(node.args[0]), tuple(sorted(over)))
        if name == "where":
            if len(node.args) != 1:
                raise DefinitionError("where 需要一个表达式参数")
            selectors = _selector_keywords(node, _SELECTOR_DIMS)
            if not selectors:
                raise DefinitionError("where 需要至少一个筛选条件")
            return Where(_convert(node.args[0]), selectors)
        if name == "apply_factor":
            if len(node.args) != 1:
                raise DefinitionError("apply_factor 需要一个表达式参数")
            activity = None
            region = "default"
            for kw in node.keywords:
                if kw.arg == "activity":
                    activity = _const_str(kw.value, "活动名称")
                elif kw.arg == "region":
                    region = _const_str(kw.value, "地区")
                else:
                    raise DefinitionError("apply_factor 只接受 activity=/region= 参数")
            if activity is None:
                raise DefinitionError("apply_factor 必须通过 activity= 指定活动")
            return ApplyFactor(_convert(node.args[0]), activity, region)
        if name == "under":
            if len(node.args) == 2:
                scenario = _const_str(node.args[1], "情景名称")
            elif len(node.args) == 1:
                scenario = None
                for kw in node.keywords:
                    if kw.arg != "scenario":
                        raise DefinitionError("under 只接受 scenario= 参数")
                    scenario = _const_str(kw.value, "情景名称")
            else:
                raise DefinitionError("under 需要表达式与情景名称")
            if scenario is None:
                raise DefinitionError("under 缺少情景名称")
            return Under(_convert(node.args[0]), scenario)
    raise DefinitionError("表达式含有不允许的构造：" + type(node).__name__)


def parse_expression(text):
    """把受限声明式表达式解析为不可变 AST。拒绝任意 Python 构造。"""
    if not isinstance(text, str) or not text.strip():
        raise DefinitionError("表达式必须为非空字符串")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise DefinitionError("表达式语法错误：%s" % exc) from exc
    return _convert(tree.body)


# ---------------------------------------------------------------- 静态检查

def _combine_grains(left, right):
    if left == right:
        return left
    if not left:
        return right
    if not right:
        return left
    raise DefinitionError("粒度不一致：%s 与 %s" % (sorted(left), sorted(right)))


def _mul_kind(dim1, kind1, dim2, kind2):
    if not dim1.powers:
        return kind2
    if not dim2.powers:
        return kind1
    return FLOW


class _Analyzer:
    """执行前检查：依赖环、量纲、粒度、可加维度。"""

    def __init__(self, kernel, scenarios):
        self.kernel = kernel
        self.scenarios = scenarios

    def sig_of(self, node, stack=()):
        kernel = self.kernel
        if isinstance(node, Num):
            return Sig(Dim(), FLOW, frozenset())
        if isinstance(node, Ref):
            if node.name not in kernel.def_asts:
                raise DefinitionError("未知指标：" + node.name)
            if node.name in stack:
                cycle = " -> ".join(list(stack) + [node.name])
                raise DefinitionError("指标依赖环：" + cycle)
            return self.sig_of(kernel.def_asts[node.name], stack + (node.name,))
        if isinstance(node, Measure):
            if node.measure not in kernel.measures:
                raise DefinitionError("未知计量：" + node.measure)
            dim, kind = kernel.measures[node.measure]
            return Sig(dim, kind, frozenset(DIMS))
        if isinstance(node, Where):
            return self.sig_of(node.expr, stack)
        if isinstance(node, Under):
            if node.scenario not in self.scenarios:
                raise DefinitionError("未在查询中声明的情景：" + node.scenario)
            return self.sig_of(node.expr, stack)
        if isinstance(node, Agg):
            sig = self.sig_of(node.expr, stack)
            unknown = set(node.over) - set(DIMS)
            if unknown:
                raise DefinitionError("未知聚合维度：" + ", ".join(sorted(unknown)))
            missing = set(node.over) - set(sig.grain)
            if missing:
                raise DefinitionError("聚合维度不在粒度中：%s（粒度为 %s）"
                                      % (sorted(missing), sorted(sig.grain)))
            if not sig.dim.is_simple:
                raise DefinitionError(
                    "复合量纲（%s）不能直接 %s 聚合；请先分别汇总分子分母再相除"
                    % (sig.dim, node.op))
            if node.op == "sum" and "period" in node.over and sig.kind == STOCK:
                raise DefinitionError("存量不能按期间求和（同单位不代表可相加）")
            return Sig(sig.dim, sig.kind, sig.grain - set(node.over))
        if isinstance(node, Bin):
            left = self.sig_of(node.left, stack)
            right = self.sig_of(node.right, stack)
            grain = _combine_grains(left.grain, right.grain)
            if node.op in ("+", "-"):
                if left.dim != right.dim:
                    raise DefinitionError("量纲不一致：%s 与 %s" % (left.dim, right.dim))
                if left.kind != right.kind:
                    raise DefinitionError("存量与流量不能直接相加减")
                return Sig(left.dim, left.kind, grain)
            if node.op == "*":
                return Sig(left.dim.mul(right.dim),
                           _mul_kind(left.dim, left.kind, right.dim, right.kind), grain)
            return Sig(left.dim.div(right.dim),
                       _mul_kind(left.dim, left.kind, right.dim, right.kind), grain)
        if isinstance(node, ApplyFactor):
            sig = self.sig_of(node.expr, stack)
            if node.activity not in kernel.factor_dims:
                raise DefinitionError("活动无可用因子：" + node.activity)
            if "period" not in sig.grain:
                raise DefinitionError("因子匹配需要 period 粒度，请先不要按期间聚合")
            return Sig(sig.dim.mul(kernel.factor_dims[node.activity]), FLOW, sig.grain)
        raise DefinitionError("未知的表达式节点：" + type(node).__name__)


# ---------------------------------------------------------------- 工具

def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(obj):
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _is_real(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _intersect(current, extra):
    if current is None:
        return extra
    if extra is None:
        return current
    return current & extra


# ---------------------------------------------------------------- 内核

class Kernel:
    """指标计算内核。构建时校验资料，之后提供固定版本的查询。"""

    def __init__(self, bundle):
        if not isinstance(bundle, dict):
            raise DataError("资料根节点必须为对象")
        self.bundle = bundle
        self.measures = self._build_measures(bundle)
        self.records = self._check_records(bundle)
        self.coverage = self._check_coverage(bundle)
        self.factors = self._check_factors(bundle)
        self.factor_dims = {}
        for factor in self.factors:
            self.factor_dims.setdefault(factor["activity"], Dim.parse(factor["unit"]))
        self.def_asts = self._parse_definitions(bundle)
        self.data_version = bundle.get("data_version")
        # 缓存身份成分：输入（记录/覆盖/计量）与因子分别成盐，
        # 使改一类输入时不误伤另一类子图的复用。
        self.data_salt = _sha({
            "records": self.records,
            "coverage": bundle.get("coverage") or [],
            "measures": {name: [str(dim), kind] for name, (dim, kind) in sorted(self.measures.items())},
        })
        self.factor_salt = _sha(self.factors)
        self._cache = {}
        self.stats = {"measure": 0, "agg": 0, "bin": 0, "factor": 0}

    @classmethod
    def from_path(cls, path):
        return cls(load_bundle(path))

    # ---------------- 资料校验 ----------------

    @staticmethod
    def _build_measures(bundle):
        registry = {}
        units = bundle.get("units") or {}
        if not isinstance(units, dict):
            raise DataError("units 必须为对象")
        for name, unit in units.items():
            registry[name] = (Dim.parse(unit), FLOW)
        measures = bundle.get("measures") or {}
        if not isinstance(measures, dict):
            raise DataError("measures 必须为对象")
        for name, meta in measures.items():
            if not isinstance(meta, dict):
                raise DataError("measures.%s 必须为对象" % name)
            dim = Dim.parse(meta.get("unit"))
            kind = meta.get("kind", FLOW)
            if kind not in (FLOW, STOCK):
                raise DataError("未知计量类别：%s（%s）" % (kind, name))
            if name in registry and registry[name][0] != dim:
                raise DataError("units 与 measures 单位不一致：" + name)
            registry[name] = (dim, kind)
        if not registry:
            raise DataError("缺少计量定义（units 或 measures）")
        return registry

    def _check_records(self, bundle):
        records = bundle.get("records")
        if not isinstance(records, list) or not records:
            raise DataError("records 必须为非空列表")
        seen_ids = set()
        seen_cells = {}
        for record in records:
            if not isinstance(record, dict):
                raise DataError("记录必须为对象")
            obs = record.get("observation_id")
            if not isinstance(obs, str) or not obs:
                raise DataError("记录缺少稳定 observation_id")
            if obs in seen_ids:
                raise DataError("observation_id 重复：" + obs)
            seen_ids.add(obs)
            boundary = record.get("boundary")
            period = record.get("period")
            if not isinstance(boundary, str) or not isinstance(period, str):
                raise DataError("记录缺少 boundary/period：" + obs)
            measure_keys = [k for k in record if k not in ("observation_id", "boundary", "period")]
            if not measure_keys:
                raise DataError("记录缺少计量字段：" + obs)
            for key in measure_keys:
                if key not in self.measures:
                    raise DataError("记录含未登记的计量字段：%s（%s）" % (key, obs))
                if not _is_real(record[key]):
                    raise DataError("计量值必须为有限数值：%s（%s）" % (key, obs))
                cell_key = (boundary, period, key)
                if cell_key in seen_cells:
                    raise DataError(
                        "同一边界/期间/计量存在多条记录（%s 与 %s），存在重复核算风险"
                        % (seen_cells[cell_key], obs))
                seen_cells[cell_key] = obs
        return records

    def _check_coverage(self, bundle):
        raw = bundle.get("coverage") or []
        if not isinstance(raw, list):
            raise DataError("coverage 必须为列表")
        groups = []
        parents = set()
        child_owner = {}
        for entry in raw:
            if not isinstance(entry, dict):
                raise DataError("覆盖关系必须为对象")
            parent = entry.get("parent")
            children = entry.get("children")
            if not isinstance(parent, str) or not parent:
                raise DataError("覆盖关系缺少 parent")
            if not isinstance(children, list) or not children or not all(isinstance(c, str) for c in children):
                raise DataError("覆盖关系 children 必须为非空字符串列表：" + parent)
            if parent in children:
                raise DataError("边界不能覆盖自身：" + parent)
            if len(set(children)) != len(children):
                raise DataError("覆盖关系 children 重复：" + parent)
            if parent in parents:
                raise DataError("同一总表边界出现在多个覆盖组：" + parent)
            if parent in child_owner:
                raise DataError("边界不能既是分表又是总表：" + parent)
            parents.add(parent)
            for child in children:
                if child in child_owner:
                    raise DataError("分表边界属于多个覆盖组：" + child)
                if child in parents:
                    raise DataError("边界不能既是分表又是总表：" + child)
                child_owner[child] = parent
            declared = entry.get("measures")
            if declared is not None:
                if not isinstance(declared, list) or not all(m in self.measures for m in declared):
                    raise DataError("覆盖关系 measures 必须为已登记计量：" + parent)
                applicable = frozenset(declared)
            else:
                # 未声明时按资料推导：总表边界出现过的计量才适用该覆盖组，
                # 避免把产量等非表计计量卷入总分表裁决。
                applicable = frozenset(
                    key for record in self.records if record["boundary"] == parent
                    for key in record if key not in ("observation_id", "boundary", "period"))
            groups.append({"parent": parent, "children": tuple(children), "measures": applicable})
        return groups

    def _check_factors(self, bundle):
        raw = bundle.get("factors") or []
        if not isinstance(raw, list):
            raise DataError("factors 必须为列表")
        factors = []
        seen_ids = set()
        activity_units = {}
        for entry in raw:
            if not isinstance(entry, dict):
                raise DataError("因子必须为对象")
            factor = dict(entry)
            fid = factor.get("factor_id")
            if not isinstance(fid, str) or not fid:
                raise DataError("因子缺少 factor_id")
            if fid in seen_ids:
                raise DataError("factor_id 重复：" + fid)
            seen_ids.add(fid)
            activity = factor.get("activity")
            if activity not in self.measures:
                raise DataError("因子活动必须是已登记计量：" + fid)
            if not _is_real(factor.get("value")):
                raise DataError("因子缺少数值 value：" + fid)
            unit = factor.get("unit")
            dim = Dim.parse(unit)
            if activity in activity_units and activity_units[activity] != dim:
                raise DataError("同一活动的因子单位不一致：" + activity)
            activity_units[activity] = dim
            region = factor.get("region", "default")
            if not isinstance(region, str):
                raise DataError("因子 region 必须为字符串：" + fid)
            priority = factor.get("priority", 0)
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise DataError("因子 priority 必须为整数：" + fid)
            version = factor.get("version")
            if version is not None and not isinstance(version, str):
                raise DataError("因子 version 必须为字符串：" + fid)
            valid_from = factor.get("valid_from")
            valid_to = factor.get("valid_to")
            for bound in (valid_from, valid_to):
                if bound is not None and not isinstance(bound, str):
                    raise DataError("因子有效区间必须为字符串：" + fid)
            if valid_from is not None and valid_to is not None and valid_from > valid_to:
                raise DataError("因子有效区间倒置：" + fid)
            factor.update(region=region, priority=priority, version=version,
                          valid_from=valid_from, valid_to=valid_to)
            factors.append(factor)
        return factors

    @staticmethod
    def _parse_definitions(bundle):
        raw = bundle.get("definitions") or []
        if not isinstance(raw, list):
            raise DataError("definitions 必须为列表")
        asts = {}
        for entry in raw:
            if not isinstance(entry, dict):
                raise DataError("指标定义必须为对象")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise DataError("指标定义缺少 name")
            if name in _BUILTINS:
                raise DataError("指标名不能与内置函数相同：" + name)
            if name in asts:
                raise DataError("指标名重复：" + name)
            asts[name] = parse_expression(entry.get("expression"))
        return asts

    # ---------------- 查询 ----------------

    def query(self, *, caliber="parent", factors=None, scenarios=None, data_version=None):
        return Query(self, caliber=caliber, factors=factors,
                     scenarios=scenarios, data_version=data_version)

    def cache_size(self):
        return len(self._cache)

    def cache_clear(self):
        self._cache.clear()
        self.reset_stats()

    def reset_stats(self):
        for key in self.stats:
            self.stats[key] = 0


# ---------------------------------------------------------------- 求值器

class _Evaluator:
    def __init__(self, query):
        self.query = query
        self.kernel = query.kernel
        self._resolutions = {}

    def eval(self, node, ctx):
        if isinstance(node, Num):
            return Frame(Sig(Dim(), FLOW, frozenset()),
                         {(): Cell(VALUE, node.value, (), ())}, ())
        if isinstance(node, Ref):
            return self.eval(self.kernel.def_asts[node.name], ctx)
        if isinstance(node, Where):
            return self.eval(node.expr, self._refine(ctx, node.selectors))
        if isinstance(node, Under):
            return self.eval(node.expr, _Ctx(ctx.boundary, ctx.period, node.scenario, ctx.caliber))
        if isinstance(node, Measure):
            return self._memo("measure", (node, ctx.cache_part(), self.kernel.data_salt),
                              lambda: self._measure(node, ctx))
        if isinstance(node, Agg):
            dep = self.eval(node.expr, ctx)
            return self._memo("agg", (node.op, node.over, ctx.cache_part(), dep.content_key),
                              lambda: self._agg(node, dep))
        if isinstance(node, Bin):
            left = self.eval(node.left, ctx)
            right = self.eval(node.right, ctx)
            return self._memo("bin", (node.op, ctx.cache_part(), left.content_key, right.content_key),
                              lambda: self._bin(node, left, right))
        if isinstance(node, ApplyFactor):
            dep = self.eval(node.expr, ctx)
            salt = self.query.ensemble_salts[ctx.scenario or BASELINE]
            return self._memo("factor", (node.activity, node.region, ctx.cache_part(),
                                         salt, dep.content_key),
                              lambda: self._apply_factor(node, dep, ctx))
        raise DefinitionError("未知的表达式节点：" + type(node).__name__)

    def _memo(self, tag, key, compute):
        key = (tag,) + key
        cached = self.kernel._cache.get(key)
        if cached is not None:
            return cached
        self.kernel.stats[tag] += 1  # 计算尝试：失败分支重算可见，且不入缓存
        frame = compute()
        self.kernel._cache[key] = frame
        return frame

    @staticmethod
    def _refine(ctx, selectors):
        boundary = ctx.boundary
        period = ctx.period
        for dim, values in selectors:
            if dim == "boundary":
                boundary = _intersect(boundary, values)
            else:
                period = _intersect(period, values)
        return _Ctx(boundary, period, ctx.scenario, ctx.caliber)

    # ---------------- 源节点：选择 + 覆盖裁决 ----------------

    def _measure(self, node, ctx):
        kernel = self.kernel
        eff_boundary = _intersect(_sel_get(node.selectors, "boundary"), ctx.boundary)
        eff_period = _intersect(_sel_get(node.selectors, "period"), ctx.period)
        candidates = [r for r in kernel.records
                      if node.measure in r
                      and (eff_boundary is None or r["boundary"] in eff_boundary)
                      and (eff_period is None or r["period"] in eff_period)]
        by_boundary = {}
        for record in candidates:
            by_boundary.setdefault(record["boundary"], []).append(record)
        kept = []
        issues = []
        consumed = set()
        hole_cells = []
        for group in kernel.coverage:
            if node.measure not in group["measures"]:
                continue
            parent = group["parent"]
            children = group["children"]
            parent_selected = eff_boundary is None or parent in eff_boundary
            parent_recs = by_boundary.get(parent, [])
            if ctx.caliber == "children":
                if eff_boundary is None:
                    expected = list(children)
                else:
                    expected = [c for c in children if c in eff_boundary]
                    if parent_selected and not expected:
                        # 只选了总表边界：在分表口径下视为要整个覆盖组的分表
                        expected = list(children)
                if parent_selected:
                    consumed.add(parent)
                    for record in parent_recs:
                        issues.append({
                            "type": "excluded-by-caliber", "caliber": "children",
                            "observation_id": record["observation_id"],
                            "boundary": parent, "period": record["period"],
                            "reason": "口径为分表，总表记录排除以避免重复核算",
                        })
                periods = set()
                for child in expected:
                    consumed.add(child)
                    for record in by_boundary.get(child, []):
                        kept.append(record)
                        periods.add(record["period"])
                for record in parent_recs:
                    periods.add(record["period"])
                for period in sorted(periods):
                    missing = [c for c in expected
                               if not any(r["period"] == period for r in by_boundary.get(c, []))]
                    if missing:
                        issues.append({
                            "type": "incomplete-children", "group": parent, "period": period,
                            "missing": sorted(missing),
                            "reason": "分表缺块，结果保留不完整范围",
                        })
            else:  # parent
                if not parent_selected:
                    continue
                consumed.add(parent)
                kept.extend(parent_recs)
                parent_periods = {r["period"] for r in parent_recs}
                holes = set()
                for child in children:
                    for record in by_boundary.get(child, []):
                        consumed.add(child)
                        issues.append({
                            "type": "excluded-by-caliber", "caliber": "parent",
                            "observation_id": record["observation_id"],
                            "boundary": child, "period": record["period"],
                            "reason": "口径为总表，分表记录排除以避免重复核算",
                        })
                        if record["period"] not in parent_periods:
                            issues.append({
                                "type": "basis-unavailable", "group": parent,
                                "period": record["period"],
                                "observation_id": record["observation_id"],
                                "reason": "口径为总表但该期间缺少总表记录，分表记录不回填",
                            })
                            holes.add(record["period"])
                for period in holes:
                    # 总表口径下该期间的计量依据缺失：以缺值单元保留坐标，
                    # 而不是让期间静默消失。
                    hole_cells.append(((parent, period),
                                       Cell(MISSING, None, (), ())))
        for record in candidates:
            if record["boundary"] not in consumed:
                kept.append(record)
        cells = {}
        for record in kept:
            coord = (record["boundary"], record["period"])
            cells[coord] = Cell(VALUE, record[node.measure],
                                (record["observation_id"],), ())
        for coord, hole in hole_cells:
            cells.setdefault(coord, hole)
        dim, kind = kernel.measures[node.measure]
        return Frame(Sig(dim, kind, frozenset(DIMS)), cells, issues)

    # ---------------- 聚合 ----------------

    def _agg(self, node, dep):
        grain = dep.sig.grain - set(node.over)
        keep = sorted(grain)
        source = sorted(dep.sig.grain)
        index = [source.index(dim) for dim in keep]
        groups = {}
        for coord, cell in dep.cells.items():
            key = tuple(coord[i] for i in index)
            groups.setdefault(key, []).append((coord, cell))
        cells = {}
        issues = list(dep.issues)
        for key, members in groups.items():
            usable = [cell for _, cell in members if cell.state == VALUE]
            obs = frozenset().union(*(cell.obs for _, cell in members))
            facs = frozenset().union(*(cell.facs for _, cell in members))
            if usable:
                if node.op == "sum":
                    value = sum(cell.value for cell in usable)
                else:
                    value = sum(cell.value for cell in usable) / len(usable)
                dropped = len(members) - len(usable)
                if dropped:
                    issues.append({
                        "type": "partial-aggregation", "op": node.op,
                        "coord": _coord_dict(grain, key), "dropped": dropped,
                        "reason": "部分坐标缺值或不可用，聚合只覆盖可用范围",
                    })
                cells[key] = Cell(VALUE, value, obs, facs)
            else:
                cells[key] = Cell(MISSING, None, obs, facs)
        if not groups and not grain:
            cells[()] = Cell(MISSING, None, (), ())
        return Frame(Sig(dep.sig.dim, dep.sig.kind, grain), cells, issues)

    # ---------------- 四则运算 ----------------

    def _bin(self, node, left, right):
        grain = _combine_grains(left.sig.grain, right.sig.grain)
        issues = list(left.issues) + list(right.issues)
        cells = {}
        if left.sig.grain == right.sig.grain:
            coords = set(left.cells) | set(right.cells)
            pairs = [(coord, left.cells.get(coord), right.cells.get(coord)) for coord in coords]
        elif not left.sig.grain:
            scalar = left.cells.get(())
            pairs = [(coord, scalar, cell) for coord, cell in right.cells.items()]
        else:
            scalar = right.cells.get(())
            pairs = [(coord, cell, scalar) for coord, cell in left.cells.items()]
        for coord, lc, rc in pairs:
            obs = (lc.obs if lc else frozenset()) | (rc.obs if rc else frozenset())
            facs = (lc.facs if lc else frozenset()) | (rc.facs if rc else frozenset())
            if lc is None or rc is None:
                issues.append({
                    "type": "operand-absent", "coord": _coord_dict(grain, coord),
                    "side": "left" if lc is None else "right",
                    "reason": "一侧操作数在该坐标无记录",
                })
                cells[coord] = Cell(MISSING, None, obs, facs)
                continue
            if lc.state != VALUE or rc.state != VALUE:
                state = MISSING if MISSING in (lc.state, rc.state) else ZERO_DENOM
                cells[coord] = Cell(state, None, obs, facs)
                continue
            a, b = lc.value, rc.value
            if node.op == "+":
                cells[coord] = Cell(VALUE, a + b, obs, facs)
            elif node.op == "-":
                cells[coord] = Cell(VALUE, a - b, obs, facs)
            elif node.op == "*":
                cells[coord] = Cell(VALUE, a * b, obs, facs)
            elif b == 0:
                cells[coord] = Cell(ZERO_DENOM, None, obs, facs)
            else:
                cells[coord] = Cell(VALUE, a / b, obs, facs)
        if node.op in ("+", "-"):
            sig = Sig(left.sig.dim, left.sig.kind, grain)
        elif node.op == "*":
            sig = Sig(left.sig.dim.mul(right.sig.dim),
                      _mul_kind(left.sig.dim, left.sig.kind, right.sig.dim, right.sig.kind), grain)
        else:
            sig = Sig(left.sig.dim.div(right.sig.dim),
                      _mul_kind(left.sig.dim, left.sig.kind, right.sig.dim, right.sig.kind), grain)
        return Frame(sig, cells, issues)

    # ---------------- 因子 ----------------

    def _apply_factor(self, node, dep, ctx):
        cells = {}
        issues = list(dep.issues)
        source = sorted(dep.sig.grain)
        period_index = source.index("period")
        for coord, cell in dep.cells.items():
            if cell.state != VALUE:
                cells[coord] = cell
                continue
            period = coord[period_index]
            factor = self._resolve(node.activity, node.region, period, ctx.scenario)
            if factor is None:
                issues.append({
                    "type": "no-factor", "activity": node.activity,
                    "region": node.region, "period": period,
                    "reason": "该期间在固定因子集内无适用因子",
                })
                cells[coord] = Cell(MISSING, None, cell.obs, cell.facs)
            else:
                cells[coord] = Cell(VALUE, cell.value * factor["value"],
                                    cell.obs, cell.facs | {factor["factor_id"]})
        dim = dep.sig.dim.mul(self.kernel.factor_dims[node.activity])
        return Frame(Sig(dim, FLOW, dep.sig.grain), cells, issues)

    def _resolve(self, activity, region, period, scenario):
        key = (activity, region, period, scenario or BASELINE)
        if key in self._resolutions:
            return self._resolutions[key]
        eligible = self.query.ensembles[scenario or BASELINE]
        matches = [f for f in eligible
                   if f["activity"] == activity and f["region"] == region
                   and (f["valid_from"] is None or f["valid_from"] <= period)
                   and (f["valid_to"] is None or period <= f["valid_to"])]
        if not matches:
            result = None
        else:
            best = max(f["priority"] for f in matches)
            top = [f for f in matches if f["priority"] == best]
            if len(top) > 1:
                raise FactorConflictError(
                    "因子冲突：活动 %s、地区 %s、期间 %s 在优先级 %d 上有多个候选：%s"
                    % (activity, region, period, best,
                       ", ".join(sorted(f["factor_id"] for f in top))))
            result = top[0]
        self._resolutions[key] = result
        return result


# ---------------------------------------------------------------- 查询

def _select_factors(factors, pin):
    if pin is None:
        return list(factors)
    if not isinstance(pin, dict):
        raise KernelError("因子固定必须为字典（versions 或 factor_ids）")
    if "versions" in pin:
        versions = pin["versions"]
        if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
            raise KernelError("versions 必须为字符串列表")
        return [f for f in factors if f["version"] in versions]
    if "factor_ids" in pin:
        ids = pin["factor_ids"]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise KernelError("factor_ids 必须为字符串列表")
        known = {f["factor_id"] for f in factors}
        unknown = sorted(set(ids) - known)
        if unknown:
            raise KernelError("未知的 factor_id：" + ", ".join(unknown))
        return [f for f in factors if f["factor_id"] in ids]
    raise KernelError("未知的因子固定方式：" + ", ".join(sorted(pin)))


def _coord_dict(grain, coord):
    return {dim: value for dim, value in zip(sorted(grain), coord)}


def frame_to_dict(frame):
    cells = []
    for coord, cell in sorted(frame.cells.items()):
        cells.append({
            "coord": _coord_dict(frame.sig.grain, coord),
            "state": cell.state,
            "value": cell.value,
            "observations": sorted(cell.obs),
            "factors": sorted(cell.facs),
        })
    return {
        "dim": str(frame.sig.dim),
        "kind": frame.sig.kind,
        "grain": sorted(frame.sig.grain),
        "cells": cells,
        "issues": list(frame.issues),
    }


def _diff_frames(base, other):
    changes = []
    for coord in sorted(set(base.cells) | set(other.cells)):
        before = base.cells.get(coord)
        after = other.cells.get(coord)
        b_state = before.state if before else None
        b_value = before.value if before else None
        a_state = after.state if after else None
        a_value = after.value if after else None
        if (b_state, b_value) == (a_state, a_value):
            continue
        delta = a_value - b_value if (b_state == VALUE and a_state == VALUE) else None
        changes.append({
            "coord": _coord_dict(base.sig.grain, coord),
            "baseline": {"state": b_state, "value": b_value},
            "scenario": {"state": a_state, "value": a_value},
            "delta": delta,
            "observations": sorted((before.obs if before else frozenset())
                                   | (after.obs if after else frozenset())),
            "factors": sorted((before.facs if before else frozenset())
                              | (after.facs if after else frozenset())),
        })
    return changes


class Query:
    """固定资料版本、口径与因子集的查询。一次查询内所有节点共享同一裁决。"""

    def __init__(self, kernel, *, caliber="parent", factors=None,
                 scenarios=None, data_version=None):
        if caliber not in CALIBERS:
            raise KernelError("口径必须为 parent 或 children：" + repr(caliber))
        if data_version is not None:
            if kernel.data_version is None:
                raise KernelError("资料未声明 data_version，无法固定资料版本")
            if data_version != kernel.data_version:
                raise KernelError("资料版本不匹配：期望 %s，实际 %s"
                                  % (data_version, kernel.data_version))
        self.kernel = kernel
        self.caliber = caliber
        self.ensembles = {BASELINE: _select_factors(kernel.factors, factors)}
        for name, pin in (scenarios or {}).items():
            if not isinstance(name, str) or not name or name == BASELINE:
                raise KernelError("情景名称必须为非空字符串且不能是 " + BASELINE)
            self.ensembles[name] = _select_factors(kernel.factors, pin)
        self.ensemble_salts = {name: _sha(factors_list)
                               for name, factors_list in self.ensembles.items()}
        self._nodes = {}
        self._frames = {}

    def _metric_nodes(self, metrics):
        if isinstance(metrics, str):
            metrics = [metrics]
        analyzer = _Analyzer(self.kernel, frozenset(self.ensembles) - {BASELINE})
        nodes = {}
        for metric in metrics:
            node = self.kernel.def_asts.get(metric)
            if node is None:
                node = parse_expression(metric)
            analyzer.sig_of(node)  # 执行前静态检查：环、量纲、粒度、可加性
            nodes[metric] = node
        return nodes

    def run(self, metrics):
        nodes = self._metric_nodes(metrics)
        evaluator = _Evaluator(self)
        result = {}
        for metric, node in nodes.items():
            frame = evaluator.eval(node, _Ctx(None, None, None, self.caliber))
            self._nodes[metric] = node
            self._frames[metric] = frame
            result[metric] = frame_to_dict(frame)
        return result

    def compare(self, metrics, scenarios):
        """批量情景比较：同一裁决下，基线与各情景逐坐标对比。"""
        if isinstance(metrics, str):
            metrics = [metrics]
        if isinstance(scenarios, str):
            scenarios = [scenarios]
        for name in scenarios:
            if name not in self.ensembles or name == BASELINE:
                raise KernelError("未声明的情景：" + repr(name))
        self.run(metrics)
        evaluator = _Evaluator(self)
        report = {}
        for metric in metrics:
            base = self._frames[metric]
            entry = {"baseline": frame_to_dict(base), "scenarios": {}}
            for name in scenarios:
                frame = evaluator.eval(self._nodes[metric],
                                       _Ctx(None, None, name, self.caliber))
                entry["scenarios"][name] = {
                    "frame": frame_to_dict(frame),
                    "changes": _diff_frames(base, frame),
                }
            report[metric] = entry
        return report

    def impact(self, factor_id, metrics):
        """因子影响：哪些结果坐标由该因子支持，对应哪些原子记录。"""
        if isinstance(metrics, str):
            metrics = [metrics]
        self.run(metrics)
        report = {}
        for metric in metrics:
            frame = self._frames[metric]
            hits = []
            for coord, cell in sorted(frame.cells.items()):
                if factor_id in cell.facs:
                    hits.append({
                        "coord": _coord_dict(frame.sig.grain, coord),
                        "state": cell.state,
                        "value": cell.value,
                        "observations": sorted(cell.obs),
                        "factors": sorted(cell.facs),
                    })
            report[metric] = hits
        return report


# ---------------------------------------------------------------- 命令行

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--metric", action="append", required=True,
                        help="指标名或表达式，可重复")
    parser.add_argument("--caliber", choices=CALIBERS, default="parent",
                        help="覆盖裁决口径：总表或分表")
    parser.add_argument("--factor-versions", nargs="*", default=None,
                        help="基线因子版本固定，如 v2026.10")
    parser.add_argument("--scenario", action="append", default=[],
                        help="情景因子固定，形如 名称=v1,v2，可重复")
    parser.add_argument("--data-version", default=None, help="固定资料版本")
    args = parser.parse_args()
    kernel = Kernel.from_path(args.path)
    factors = {"versions": args.factor_versions} if args.factor_versions else None
    scenarios = {}
    for item in args.scenario:
        name, sep, versions = item.partition("=")
        if not sep or not name:
            raise SystemExit("情景参数形如 名称=v1,v2：" + item)
        scenarios[name] = {"versions": [v for v in versions.split(",") if v]}
    query = kernel.query(caliber=args.caliber, factors=factors,
                         scenarios=scenarios, data_version=args.data_version)
    print(json.dumps(query.run(args.metric), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
