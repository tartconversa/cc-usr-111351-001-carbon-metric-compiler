"""指标计算内核的回归检查。

覆盖：强度由分子分母重算、总分表裁决、局部缺表、因子冲突与版本、
存量/流量可加性、菱形依赖复用、筛选下推、失败分支缓存、情景差值。
"""
import unittest
from pathlib import Path

from src.carbon_metric_compiler_kernel import (
    CALIBERS,
    DataError,
    DefinitionError,
    FactorConflictError,
    Kernel,
    KernelError,
    parse_expression,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kernel_sample.json"


def bundle(records, coverage=None, units=None, measures=None, factors=None,
           definitions=None, **extra):
    data = {
        "schema_version": 1,
        "records": records,
        "coverage": coverage or [],
        "units": units or {},
        "factors": factors or [],
        "definitions": definitions or [],
    }
    if measures is not None:
        data["measures"] = measures
    data.update(extra)
    return data


def cell(result, metric, **coord):
    for entry in result[metric]["cells"]:
        if all(entry["coord"].get(k) == v for k, v in coord.items()):
            return entry
    raise AssertionError("坐标不存在：%s %s" % (metric, coord))


def issue_types(result, metric):
    return [issue["type"] for issue in result[metric]["issues"]]


WORKSHOPS = bundle(
    records=[
        {"observation_id": "a", "boundary": "workshop-a", "period": "2026-10",
         "emission": 10, "output": 2},
        {"observation_id": "b", "boundary": "workshop-b", "period": "2026-10",
         "emission": 90, "output": 30},
    ],
    coverage=[{"parent": "park", "children": ["workshop-a", "workshop-b"]}],
    units={"emission": "kgCO2e", "output": "t"},
    definitions=[
        {"name": "em", "expression": "sum(measure(\"emission\"), over=[\"boundary\"])"},
        {"name": "out", "expression": "sum(measure(\"output\"), over=[\"boundary\"])"},
        {"name": "intensity", "expression": "em / out"},
    ],
)


class IntensityTests(unittest.TestCase):
    def test_intensity_recomputed_from_parts(self):
        # 10/2 与 90/30 的整体强度必须是 100/32，不能是 (5+3)/2=4
        result = Kernel(WORKSHOPS).query().run(["intensity"])
        entry = cell(result, "intensity", period="2026-10")
        self.assertEqual(entry["state"], "value")
        self.assertAlmostEqual(entry["value"], 100 / 32)
        self.assertNotAlmostEqual(entry["value"], 4.0)
        self.assertEqual(entry["observations"], ["a", "b"])
        self.assertEqual(result["intensity"]["dim"], "kgCO2e/t")

    def test_ratio_aggregation_rejected_statically(self):
        kernel = Kernel(WORKSHOPS)
        for text in ("mean(measure(\"emission\") / measure(\"output\"), over=[\"boundary\"])",
                     "sum(measure(\"emission\") / measure(\"output\"), over=[\"boundary\"])"):
            with self.assertRaises(DefinitionError):
                kernel.query().run([text])

    def test_filter_pushes_down_to_atoms(self):
        kernel = Kernel(WORKSHOPS)
        result = kernel.query().run([
            "where(intensity, boundary=[\"workshop-a\"])",
            "where(intensity, boundary=[\"workshop-b\"])",
        ])
        a = cell(result, "where(intensity, boundary=[\"workshop-a\"])", period="2026-10")
        b = cell(result, "where(intensity, boundary=[\"workshop-b\"])", period="2026-10")
        # 比率由筛选后的原子记录重算，而不是在算完的比率上删行
        self.assertAlmostEqual(a["value"], 5.0)
        self.assertEqual(a["observations"], ["a"])
        self.assertAlmostEqual(b["value"], 3.0)
        self.assertEqual(b["observations"], ["b"])


METERS = bundle(
    records=[
        {"observation_id": "m-park-p1", "boundary": "park", "period": "2026-10",
         "electricity": 100},
        {"observation_id": "m-wa-p1", "boundary": "workshop-a", "period": "2026-10",
         "electricity": 40},
        {"observation_id": "m-wb-p1", "boundary": "workshop-b", "period": "2026-10",
         "electricity": 60},
        {"observation_id": "m-park-p2", "boundary": "park", "period": "2026-11",
         "electricity": 100},
        {"observation_id": "m-wa-p2", "boundary": "workshop-a", "period": "2026-11",
         "electricity": 40},
    ],
    coverage=[{"parent": "park", "children": ["workshop-a", "workshop-b"]}],
    units={"electricity": "kWh"},
    definitions=[
        {"name": "total", "expression": "sum(measure(\"electricity\"), over=[\"boundary\"])"},
    ],
)


class CoverageTests(unittest.TestCase):
    def test_parent_caliber_never_double_counts(self):
        result = Kernel(METERS).query(caliber="parent").run(["total"])
        # 总表 100 与分表 40+60 不能得到 200
        self.assertEqual(cell(result, "total", period="2026-10")["value"], 100)
        self.assertEqual(cell(result, "total", period="2026-11")["value"], 100)
        excluded = [i for i in result["total"]["issues"] if i["type"] == "excluded-by-caliber"]
        self.assertEqual({i["observation_id"] for i in excluded},
                         {"m-wa-p1", "m-wb-p1", "m-wa-p2"})

    def test_children_caliber_excludes_parent(self):
        result = Kernel(METERS).query(caliber="children").run(["total"])
        self.assertEqual(cell(result, "total", period="2026-10")["value"], 100)
        excluded = [i for i in result["total"]["issues"] if i["type"] == "excluded-by-caliber"]
        self.assertTrue(any(i["observation_id"] == "m-park-p1" for i in excluded))

    def test_partial_children_keep_incomplete_range(self):
        result = Kernel(METERS).query(caliber="children").run(["total"])
        # 2026-11 缺 workshop-b 分表：保留不完整范围并显式标记
        entry = cell(result, "total", period="2026-11")
        self.assertEqual(entry["state"], "value")
        self.assertEqual(entry["value"], 40)
        incomplete = [i for i in result["total"]["issues"] if i["type"] == "incomplete-children"]
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]["missing"], ["workshop-b"])
        self.assertEqual(incomplete[0]["period"], "2026-11")

    def test_parent_caliber_does_not_fall_back(self):
        records = [r for r in METERS["records"] if r["observation_id"] != "m-park-p2"]
        data = bundle(records=records, coverage=METERS["coverage"],
                      units={"electricity": "kWh"}, definitions=METERS["definitions"])
        result = Kernel(data).query(caliber="parent").run(["total"])
        # 总表口径下 2026-11 无总表记录：分表不回填，坐标缺值并说明
        self.assertEqual(cell(result, "total", period="2026-11")["state"], "missing")
        self.assertIn("basis-unavailable", issue_types(result, "total"))

    def test_duplicate_cell_records_rejected(self):
        data = bundle(
            records=[
                {"observation_id": "x1", "boundary": "b", "period": "p", "electricity": 1},
                {"observation_id": "x2", "boundary": "b", "period": "p", "electricity": 2},
            ],
            units={"electricity": "kWh"},
        )
        with self.assertRaises(DataError):
            Kernel(data)


def factor(fid, value, version=None, priority=0, valid_from=None, valid_to=None,
           activity="electricity", region="default"):
    return {
        "factor_id": fid, "activity": activity, "region": region,
        "unit": "kgCO2e/kWh", "value": value, "priority": priority,
        "version": version, "valid_from": valid_from, "valid_to": valid_to,
    }


FACTOR_BUNDLE = bundle(
    records=[
        {"observation_id": "e-p1", "boundary": "site", "period": "2026-10",
         "electricity": 100, "output": 50},
        {"observation_id": "e-p2", "boundary": "site", "period": "2026-11",
         "electricity": 200, "output": 80},
    ],
    units={"electricity": "kWh", "output": "t"},
    factors=[
        factor("f-old", 0.5, version="v1", valid_from="2026-01", valid_to="2026-12"),
        factor("f-new", 0.6, version="v2", valid_from="2026-01", valid_to="2026-12"),
    ],
    definitions=[
        {"name": "emission", "expression":
         "sum(apply_factor(measure(\"electricity\"), activity=\"electricity\"), over=[\"boundary\"])"},
        {"name": "production", "expression": "sum(measure(\"output\"), over=[\"boundary\"])"},
        {"name": "delta", "expression": "under(emission, \"new\") - emission"},
    ],
)


class FactorTests(unittest.TestCase):
    def test_same_priority_overlap_is_conflict(self):
        kernel = Kernel(FACTOR_BUNDLE)
        with self.assertRaises(FactorConflictError) as caught:
            kernel.query().run(["emission"])
        message = str(caught.exception)
        self.assertIn("f-old", message)
        self.assertIn("f-new", message)

    def test_priority_resolves_overlap(self):
        data = bundle(records=FACTOR_BUNDLE["records"],
                      units={"electricity": "kWh", "output": "t"},
                      factors=[factor("f-low", 0.5, priority=0),
                               factor("f-high", 0.7, priority=1)],
                      definitions=FACTOR_BUNDLE["definitions"])
        result = Kernel(data).query().run(["emission"])
        self.assertAlmostEqual(cell(result, "emission", period="2026-10")["value"], 70.0)

    def test_validity_intervals_pick_per_period(self):
        data = bundle(records=FACTOR_BUNDLE["records"],
                      units={"electricity": "kWh", "output": "t"},
                      factors=[factor("f-1", 0.5, version="v1",
                                      valid_from="2026-01", valid_to="2026-10"),
                               factor("f-2", 0.6, version="v2",
                                      valid_from="2026-11", valid_to="2026-12")],
                      definitions=FACTOR_BUNDLE["definitions"])
        result = Kernel(data).query(factors={"versions": ["v1", "v2"]}).run(["emission"])
        self.assertAlmostEqual(cell(result, "emission", period="2026-10")["value"], 50.0)
        self.assertAlmostEqual(cell(result, "emission", period="2026-11")["value"], 120.0)

    def test_missing_factor_period_is_missing_cell(self):
        data = bundle(records=FACTOR_BUNDLE["records"],
                      units={"electricity": "kWh", "output": "t"},
                      factors=[factor("f-1", 0.5, version="v1",
                                      valid_from="2026-01", valid_to="2026-10")],
                      definitions=FACTOR_BUNDLE["definitions"])
        result = Kernel(data).query(factors={"versions": ["v1"]}).run(["emission"])
        self.assertEqual(cell(result, "emission", period="2026-11")["state"], "missing")
        self.assertIn("no-factor", issue_types(result, "emission"))

    def test_scenario_compare_and_reuse(self):
        kernel = Kernel(FACTOR_BUNDLE)
        query = kernel.query(factors={"versions": ["v1"]},
                             scenarios={"new": {"versions": ["v2"]}})
        query.run(["emission", "production"])
        measure_before = kernel.stats["measure"]
        report = query.compare(["emission", "production"], scenarios=["new"])
        change = report["emission"]["scenarios"]["new"]["changes"][0]
        self.assertEqual(change["coord"], {"period": "2026-10"})
        self.assertAlmostEqual(change["baseline"]["value"], 50.0)
        self.assertAlmostEqual(change["scenario"]["value"], 60.0)
        self.assertAlmostEqual(change["delta"], 10.0)
        self.assertEqual(change["observations"], ["e-p1"])
        self.assertEqual(sorted(change["factors"]), ["f-new", "f-old"])
        # 无因子子图在情景间复用：计量源节点没有重算
        self.assertEqual(kernel.stats["measure"], measure_before)
        self.assertEqual(report["production"]["scenarios"]["new"]["changes"], [])

    def test_impact_lists_supporting_atoms(self):
        kernel = Kernel(FACTOR_BUNDLE)
        query = kernel.query(factors={"versions": ["v1"]})
        report = query.impact("f-old", ["emission", "production"])
        self.assertEqual(len(report["emission"]), 2)
        self.assertEqual(report["emission"][0]["observations"], ["e-p1"])
        self.assertEqual(report["production"], [])
        self.assertEqual(query.impact("f-new", ["emission"])["emission"], [])

    def test_scenario_delta_definition(self):
        kernel = Kernel(FACTOR_BUNDLE)
        query = kernel.query(factors={"versions": ["v1"]},
                             scenarios={"new": {"versions": ["v2"]}})
        result = query.run(["delta"])
        self.assertAlmostEqual(cell(result, "delta", period="2026-10")["value"], 10.0)
        self.assertEqual(result["delta"]["dim"], "kgCO2e")

    def test_failed_branch_leaves_no_cache(self):
        kernel = Kernel(FACTOR_BUNDLE)
        with self.assertRaises(FactorConflictError):
            kernel.query().run(["emission"])
        size_after_first = kernel.cache_size()
        with self.assertRaises(FactorConflictError):
            kernel.query().run(["emission"])
        # 失败分支重算而非命中缓存，且不向缓存写入任何新条目
        self.assertEqual(kernel.stats["factor"], 2)
        self.assertEqual(kernel.cache_size(), size_after_first)
        ok = kernel.query(factors={"versions": ["v1"]}).run(["emission"])
        self.assertAlmostEqual(cell(ok, "emission", period="2026-10")["value"], 50.0)


STOCK_BUNDLE = bundle(
    records=[
        {"observation_id": "s-a-p1", "boundary": "tank-a", "period": "2026-10", "inventory": 5},
        {"observation_id": "s-a-p2", "boundary": "tank-a", "period": "2026-11", "inventory": 7},
        {"observation_id": "s-b-p1", "boundary": "tank-b", "period": "2026-10", "inventory": 11},
    ],
    measures={"inventory": {"unit": "t", "kind": "stock"}},
    units={"inventory": "t"},
)


class AdditivityTests(unittest.TestCase):
    def test_stock_not_summable_over_period(self):
        kernel = Kernel(STOCK_BUNDLE)
        with self.assertRaises(DefinitionError) as caught:
            kernel.query().run(["sum(measure(\"inventory\"), over=[\"period\"])"])
        self.assertIn("存量", str(caught.exception))

    def test_stock_summable_over_boundary(self):
        result = Kernel(STOCK_BUNDLE).query().run(
            ["sum(measure(\"inventory\"), over=[\"boundary\"])"])
        self.assertEqual(cell(result, "sum(measure(\"inventory\"), over=[\"boundary\"])",
                              period="2026-10")["value"], 16)

    def test_stock_mean_over_period_allowed(self):
        result = Kernel(STOCK_BUNDLE).query().run(
            ["mean(measure(\"inventory\", boundary=[\"tank-a\"]), over=[\"period\"])"])
        self.assertEqual(cell(result, "mean(measure(\"inventory\", boundary=[\"tank-a\"]), over=[\"period\"])",
                              boundary="tank-a")["value"], 6)

    def test_dimension_mismatch_rejected(self):
        with self.assertRaises(DefinitionError):
            Kernel(WORKSHOPS).query().run(
                ["measure(\"emission\") + measure(\"output\")"])

    def test_grain_mismatch_rejected(self):
        with self.assertRaises(DefinitionError):
            Kernel(WORKSHOPS).query().run(
                ["measure(\"emission\") + sum(measure(\"emission\"), over=[\"boundary\"])"])


class GraphTests(unittest.TestCase):
    def test_diamond_dependency_computed_once(self):
        data = bundle(
            records=[{"observation_id": "o1", "boundary": "s", "period": "2026-10", "output": 32}],
            units={"output": "t"},
            definitions=[
                {"name": "base", "expression": "sum(measure(\"output\"), over=[\"boundary\"])"},
                {"name": "b2", "expression": "base * 2"},
                {"name": "b3", "expression": "base * 3"},
                {"name": "top", "expression": "b2 + b3"},
            ],
        )
        kernel = Kernel(data)
        result = kernel.query().run(["top"])
        self.assertEqual(cell(result, "top", period="2026-10")["value"], 160)
        self.assertEqual(kernel.stats["measure"], 1)

    def test_cycle_rejected(self):
        data = bundle(
            records=[{"observation_id": "o1", "boundary": "s", "period": "p", "output": 1}],
            units={"output": "t"},
            definitions=[
                {"name": "x", "expression": "y + 1"},
                {"name": "y", "expression": "x + 1"},
            ],
        )
        with self.assertRaises(DefinitionError) as caught:
            Kernel(data).query().run(["x"])
        self.assertIn("依赖环", str(caught.exception))

    def test_unknown_references_rejected(self):
        kernel = Kernel(WORKSHOPS)
        with self.assertRaises(DefinitionError):
            kernel.query().run(["no_such_metric"])
        with self.assertRaises(DefinitionError):
            kernel.query().run(["measure(\"no_such_measure\")"])

    def test_undeclared_scenario_rejected(self):
        kernel = Kernel(FACTOR_BUNDLE)
        with self.assertRaises(DefinitionError):
            kernel.query(factors={"versions": ["v1"]}).run(["delta"])


class StateTests(unittest.TestCase):
    def test_missing_zero_and_zero_denominator_distinct(self):
        data = bundle(
            records=[
                {"observation_id": "z1", "boundary": "c", "period": "p1",
                 "emission": 5, "output": 0},
                {"observation_id": "z2", "boundary": "d", "period": "p1",
                 "emission": 0, "output": 4},
            ],
            units={"emission": "kgCO2e", "output": "t"},
        )
        kernel = Kernel(data)
        result = kernel.query().run([
            "measure(\"emission\") / measure(\"output\")",
            "sum(measure(\"emission\", boundary=[\"nowhere\"]), over=[\"boundary\", \"period\"])",
        ])
        ratio = "measure(\"emission\") / measure(\"output\")"
        zero_denom = cell(result, ratio, boundary="c", period="p1")
        self.assertEqual(zero_denom["state"], "zero_denominator")
        self.assertIsNone(zero_denom["value"])
        effective_zero = cell(result, ratio, boundary="d", period="p1")
        self.assertEqual(effective_zero["state"], "value")
        self.assertEqual(effective_zero["value"], 0)
        missing = result["sum(measure(\"emission\", boundary=[\"nowhere\"]), over=[\"boundary\", \"period\"])"]["cells"]
        self.assertEqual(missing[0]["state"], "missing")
        self.assertIsNone(missing[0]["value"])

    def test_ragged_operands_become_missing(self):
        data = bundle(
            records=[
                {"observation_id": "r1", "boundary": "s", "period": "p1", "a": 1, "b": 2},
                {"observation_id": "r2", "boundary": "s", "period": "p2", "a": 3},
            ],
            units={"a": "t", "b": "t"},
        )
        result = Kernel(data).query().run(["measure(\"a\") - measure(\"b\")"])
        self.assertEqual(cell(result, "measure(\"a\") - measure(\"b\")",
                              boundary="s", period="p2")["state"], "missing")


class ParserTests(unittest.TestCase):
    def test_arbitrary_python_rejected(self):
        for text in ("__import__(\"os\").system(\"id\")",
                     "open(\"/etc/passwd\").read()",
                     "().__class__",
                     "[x for x in y]",
                     "f\"{1}\"",
                     "lambda: 1",
                     "measure(\"electricity\") if True else 0",
                     "sum(measure(\"electricity\"))"):
            with self.assertRaises(DefinitionError, msg=text):
                parse_expression(text)

    def test_valid_forms_parse(self):
        for text in ("1 + 2 * 3",
                     "-measure(\"electricity\")",
                     "sum(measure(\"electricity\", boundary=\"a\"), over=[\"boundary\"])",
                     "under(x, \"s\")",
                     "under(x, scenario=\"s\")"):
            parse_expression(text)


class QueryInterfaceTests(unittest.TestCase):
    def test_data_version_pin(self):
        data = bundle(records=WORKSHOPS["records"], units={"emission": "kgCO2e", "output": "t"},
                      definitions=WORKSHOPS["definitions"], data_version="v-2026-11")
        kernel = Kernel(data)
        kernel.query(data_version="v-2026-11").run(["intensity"])
        with self.assertRaises(KernelError):
            kernel.query(data_version="v-old")
        with self.assertRaises(KernelError):
            Kernel(WORKSHOPS).query(data_version="v-2026-11")

    def test_caliber_validation(self):
        kernel = Kernel(WORKSHOPS)
        with self.assertRaises(KernelError):
            kernel.query(caliber="mixed")
        for caliber in CALIBERS:
            kernel.query(caliber=caliber)

    def test_unknown_factor_pin_rejected(self):
        kernel = Kernel(FACTOR_BUNDLE)
        with self.assertRaises(KernelError):
            kernel.query(factors={"factor_ids": ["nope"]})


class FixtureTests(unittest.TestCase):
    def test_kernel_sample_end_to_end(self):
        kernel = Kernel.from_path(FIXTURE)
        query = kernel.query(caliber="children",
                             factors={"versions": ["v2026.10"]},
                             scenarios={"new_factor": {"versions": ["v2026.11"]}},
                             data_version="demo-2026-11")
        result = query.run(["park_electricity", "park_intensity", "intensity_delta"])
        electricity = result["park_electricity"]
        self.assertEqual(cell(result, "park_electricity", period="2026-10")["value"], 100)
        # 局部缺表：2026-11 缺 workshop-b，保留不完整范围
        nov = cell(result, "park_electricity", period="2026-11")
        self.assertEqual(nov["value"], 40)
        self.assertIn("incomplete-children", issue_types(result, "park_electricity"))
        # 强度由分子分母重算：50 / 32
        self.assertAlmostEqual(cell(result, "park_intensity", period="2026-10")["value"],
                               50 / 32)
        # 情景差值：因子 0.5 -> 0.6，排放差 10，强度差 10/32
        self.assertAlmostEqual(cell(result, "intensity_delta", period="2026-10")["value"],
                               10 / 32)
        self.assertEqual(electricity["dim"], "kWh")

    def test_parent_caliber_on_fixture(self):
        kernel = Kernel.from_path(FIXTURE)
        query = kernel.query(caliber="parent", factors={"versions": ["v2026.10"]})
        result = query.run(["park_electricity"])
        self.assertEqual(cell(result, "park_electricity", period="2026-10")["value"], 100)
        self.assertEqual(cell(result, "park_electricity", period="2026-11")["value"], 100)


if __name__ == "__main__":
    unittest.main()
