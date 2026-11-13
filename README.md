# 园区碳指标的维度语义编译与情景重算

计量资料载入模块。读取车间活动、覆盖关系和口径定义，保留原始数值及资料版本。

当前代码提供JSON资料结构读取与字段存在性检查，保留原始数据。
fixtures/sample.json 使用合成记录；读取时不执行推断、匹配、规划或核算。
src/carbon_metric_compiler_input.py 是资料读取入口，tests/test_input.py 检查结构解析和错误处理。
schema_version 当前为整数1，主集合为 records；其余字段保留在返回的字典中。
此版本没有第三方运行依赖，使用 Python 3.14。

读取示例：

    python -m src.carbon_metric_compiler_input fixtures/sample.json

运行资料读取检查：

    python -m unittest discover -s tests -v

检查源码可编译性：

    python -m compileall -q src tests
