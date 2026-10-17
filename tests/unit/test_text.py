"""app/domain/text.py 的单测。

这一层是「纯函数」测试：不联网、不读文件、不起服务，整个文件跑完在毫秒级。
这正是 ARCHITECTURE.md §2 原则 P1 想要的效果 —— 把最难写的业务规则放进
一个「测起来毫不费力」的位置。
"""

import pytest

from app.domain.text import (
    is_current_marker,
    merge_experience_months,
    months_between,
    parse_salary_range,
    parse_year_month,
)


# @pytest.mark.parametrize 的用法：
#   把「一组输入 → 期望输出」写成一张表，pytest 会为每一行单独生成一个测试用例。
#   好处是某一行失败时，报错信息直接告诉你是哪一组输入挂了，
#   而不用在十几个 assert 里逐个二分查找。
# 第一个参数是「参数名元组」，第二个参数是「一组组的取值」。
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ---- 标准写法 ----
        ("2020-07", "2020-07"),
        # ---- 各种分隔符 ----
        ("2020.07", "2020-07"),   # 参考项目在这里算出了起始年 0
        ("2020/06", "2020-06"),
        ("2020 07", "2020-07"),   # 空格分隔
        ("2020-7", "2020-07"),    # 月份一位数要补零
        # ---- 中文写法 ----
        ("2020年6月", "2020-06"),
        ("2020年10月", "2020-10"),
        # ---- 只写到年：按约定补成当年 1 月 ----
        ("2020", "2020-01"),
        ("2020年", "2020-01"),
        # ---- 首尾空白：应被容忍（简历里从 PDF 抽出来的文本常带空白）----
        ("  2020-07 ", "2020-07"),
    ],
)
def test_parse_year_month_accepts(raw, expected):
    """能确定的写法必须解析出确定的结果。"""
    assert parse_year_month(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,             # 上游根本没给值
        "",               # 空串
        "   ",            # 纯空白
        "至今",           # 这是「在职中」的标记，不是日期
        "present",
        "abc-2020",       # 带杂质：参考项目在这里解析出了「起始年 0」
        "2020年入职",      # 带杂质
        "2020-07-15",     # 精确到日，超出本系统的「月」粒度
        "2020-13",        # 13 月不存在
        "2020-00",        # 0 月不存在
        "0000",           # 年份越界
        "9999",           # 年份越界
    ],
)
def test_parse_year_month_rejects(raw):
    """无法确定的一律返回 None —— 绝不猜一个值出来。

    这个函数是「宁缺毋滥」的守门人：它退回 None，上层才有机会
    按 unknown_field_policy 决定是「宽松放行」还是「严格淘汰」。
    如果它悄悄猜了个值，上层就永远失去做这个决定的机会。
    """
    assert parse_year_month(raw) is None


# ============================================================
# is_current_marker：识别「在职中」
# ============================================================

@pytest.mark.parametrize(
    "raw",
    ["至今", "  至今  ", "至今为止", "现在", "目前", "在职", "在职中", "当前",
     "present", "Present", "PRESENT", "now", "current"],
)
def test_is_current_marker_accepts(raw):
    assert is_current_marker(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        None,          # 上游没给值
        "",            # 空串 —— 没写结束时间 ≠ 干到今天
        "   ",         # 纯空白
        "今",          # 单字太容易误伤，不接受
        "2020-07",     # 这是日期，不是标记
        "present day", # 只认整串
    ],
)
def test_is_current_marker_rejects(raw):
    assert is_current_marker(raw) is False


# ============================================================
# months_between：两个年月之间几个月
# ============================================================

@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2020-01", "2020-02", 1),      # 相邻两个月 = 1
        ("2025-08", "2026-09", 13),     # ARCHITECTURE.md §4.4 的例子
        ("2020-01", "2020-01", 0),      # 同一个月，零跨度
        ("2020.07", "2023.12", 41),     # 脏写法也支持：复用 parse_year_month
        ("2020年6月", "2021年6月", 12),
        ("2020-01", "2019-12", None),   # 倒挂：数据有误
        (None, "2020-01", None),        # 缺开始
        ("2020-01", None, None),        # 缺结束
        ("abc", "2020-01", None),       # 垃圾数据
    ],
)
def test_months_between(start, end, expected):
    assert months_between(start, end) == expected


# ============================================================
# merge_experience_months：多段经历取并集
# ============================================================

def test_merge_empty_list_is_zero():
    """没有任何经历 = 0 个月，不是 None。"""
    assert merge_experience_months([], "2026-10") == 0


def test_merge_single_period():
    assert merge_experience_months([("2020-01", "2020-07", False)], "2026-10") == 6


def test_merge_non_overlapping_adds_up():
    """不重叠的两段：12 + 12 = 24。"""
    periods = [("2018-01", "2019-01", False), ("2020-01", "2021-01", False)]
    assert merge_experience_months(periods, "2026-10") == 24


def test_merge_overlapping_uses_union_not_sum():
    """重叠的部分只算一次 —— 参考项目已知未修的问题。

    直接相加：11 + 12 = 23
    取并集  ：2020-01 ~ 2021-06 = 17
    """
    periods = [("2020-01", "2020-12", False), ("2020-06", "2021-06", False)]
    assert merge_experience_months(periods, "2026-10") == 17


def test_merge_current_period_uses_today():
    """2020-07 至今，今天 2026-10 → 75 个月。"""
    assert merge_experience_months([("2020-07", None, True)], "2026-10") == 75


def test_merge_recognizes_current_marker_in_end_field():
    """即使 is_current 传了 False，结束时间写着「至今」也按在职处理。

    防御的是上游抽取失败、只把原文写进 end 字段的情况：
    宁可多算，也不让这段经历被整段丢掉。
    """
    assert merge_experience_months([("2020-07", "至今", False)], "2026-10") == 75


def test_merge_skips_invalid_segments():
    """无效的段跳过，不影响有效的段。"""
    periods = [
        ("abc", "2020-01", False),      # 开始无效
        ("2020-01", "2019-01", False),  # 倒挂
        ("2020-01", None, False),       # 缺结束且非在职
        ("2020-01", "2020-06", False),  # 唯一有效：5 个月
    ]
    assert merge_experience_months(periods, "2026-10") == 5


def test_merge_rejects_invalid_today():
    """today 由程序传入，格式错误是编程错误，必须抛异常而不是静默归零。"""
    with pytest.raises(ValueError):
        merge_experience_months([], "不是日期")


# ============================================================
# parse_salary_range：薪资写法 → (最低月薪, 最高月薪)
# ============================================================

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20K-30K", (20000, 30000)),
        ("20-30k", (20000, 30000)),          # 小写、单点分隔
        ("20~30K", (20000, 30000)),
        ("2万-3万", (20000, 30000)),
        ("20000-30000", (20000, 30000)),
        ("20K", (20000, 20000)),             # 只写一个数
        (" 25 K ", (25000, 25000)),          # 空白容忍
        ("30K-20K", (20000, 30000)),         # 写反了，自动纠正
    ],
)
def test_parse_salary_range_accepts(raw, expected):
    assert parse_salary_range(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "面议",
        "negotiable",
        "年薪 40 万",     # 年薪换算成月薪要除以 12，本系统不做这个假设
        "20万/年",
        "没有数字的文本",
    ],
)
def test_parse_salary_range_rejects(raw):
    assert parse_salary_range(raw) is None
