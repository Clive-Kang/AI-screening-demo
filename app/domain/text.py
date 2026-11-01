"""领域层的纯文本工具：字符串进，字符串出，不碰 IO，不碰框架。

本模块只做一件事 —— 把简历里的脏文本洗干净。所有解析函数遵守同一条纪律：

    能确定就返回确定的结果，不能确定就返回 None，绝不猜。

参考项目在日期上连踩两次坑（'2020.07' 被解析成起始年 0、'至今' 的年份被硬编码成 2025），
根因都是同一个：解析失败时"猜"了一个值出来。猜错的数据不会报错，
它会安静地流进评分和报告里，让结果看起来有据可查，实际上毫无依据。
"""

import re
from collections.abc import Sequence  

# 年份的合理区间。超出即判为脏数据，直接返回 None，不让它污染下游的年限统计。
# 简历里不可能出现 1899 年之前的工作经历；2100 只是给未来留的余量。
_MIN_YEAR = 1900
_MAX_YEAR = 2100

# 利用正则表达式 定义日期串的整体形状：4 位年 + 可选的分隔符与月 + 可选的“年/月”字。
# 逐段拆开看：
#
#   ^\s*                    行首允许有空白
#   (?P<year>\d{4})         4 位年份，用命名组捕获
#   \s*                     年份后允许空白
#   (?:                     ┌ 以下几段整体可选：没写月份时整块跳过
#       [-./年\s]+            分隔符：短横线、点、斜杠、空白、或“年”字（可连续）
#       \s*
#       (?P<month>\d{1,2})    1-2 位月份
#       \s*月?                月份后允许空白和“月”字
#   )?                      └
#   \s*年?                  尾部容错：允许一个孤零零的“年”字（覆盖只写到年的 '2020年'）
#   \s*$                    行尾允许空白，且必须到此结束
#
# 关键在最外层的 ^ 和 $：它们要求「整串」匹配。
# 所以 'abc-2020'、'2020年入职' 会因为带杂质而整体失败，
# 而不是被正则从中间「挖」出一段来当成结果 —— 参考项目正是这样把 'abc-2020'
# 解析出了结果，才导致垃圾数据一路流到评分。
_YM_PATTERN = re.compile(
    r"^\s*(?P<year>\d{4})\s*(?:[-./年\s]+\s*(?P<month>\d{1,2})\s*月?)?\s*年?\s*$"
)


def parse_year_month(raw: str | None) -> str | None: 
    """把简历里五花八门的日期写法，统一成 'YYYY-MM'。

    解析不了就返回 None —— 不猜、不兜底、不抛异常。

    参数:
        raw: 原始字符串，来自简历正文或 LLM 抽取结果。
             非字符串（None 等）与空串一律视为解析失败。

    返回:
        'YYYY-MM' 格式的字符串；无法确定时返回 None。

    接受的写法:
        '2020-07'  '2020.07'  '2020/06'  '2020 07'  '2020-7'
        '2020年6月'  '2020年10月'
        '2020'  '2020年'        （只写到年 → 按下限补成当年 1 月）

    明确拒绝的写法:
        '至今' / 'present' / ''  —— 它们是「在职中」的标记，不是日期，
                                     识别工作由 is_current_marker 承担
        '2020-07-15'              —— 精确到日的日期，超出本系统「月」的粒度
        '2020-13'  '2020-00'      —— 月份越界
        'abc-2020'  '2020年入职'   —— 带杂质，无法确定真实含义
        '0000'  '9999'            —— 年份越界
    """
    # 非字符串（None、数字等）直接判失败。
    # 这里刻意不做 str(raw) 转换：静默转换会掩盖上游的类型错误，
    # 让一个本该暴露的数据问题变成一条看起来正常的结果。
    if not isinstance(raw, str): ## ① 不是字符串 → None
        return None

    matched = _YM_PATTERN.match(raw) # ② 整串匹配
    if matched is None:
        return None           #    没匹配上 → None 

    year = int(matched.group("year"))  # ③ 取年，检查 1900~2100
    if not (_MIN_YEAR <= year <= _MAX_YEAR):
        return None

    month_text = matched.group("month")
    if month_text is None:
        # 只写到年（'2020'、'2020年'）时，按下限补成当年 1 月。
        # 代价是若实际是当年 12 月，最多低估 11 个月；
        # 收益是输出永远是干净的 YYYY-MM，下游任何地方都不必再处理
        # 「有年无月」这个中间状态 —— 少一种状态就少一类 bug。
        month = 1
    else:
        month = int(month_text)
        if not (1 <= month <= 12):
            return None

    return f"{year}-{month:02d}"


# ============================================================
# 经历时长计算
#
# 这一节回答一个问题：一个人一共干了多久？
# 三个坑必须一次解决：日期写法杂（上一节解决）、"至今"怎么算（依赖今天）、
# 多段经历时间重叠时不能重复累加（参考项目已知未修的问题）。
# ============================================================

# 「在职中」的标记词。统一小写、去首尾空白后做**整串**比对，不做模糊匹配——
# "至今为止的思考"这种文本不该被当成在职中。
_CURRENT_MARKERS = frozenset({     #frozenset：不可变集合，适合做常量查找，查询 O (1)
    "至今", "至今为止", "现在", "目前", "在职", "在职中", "当前",
    "present", "now", "current",
})

# 一段经历的轻量表示：(开始年月, 结束年月, 是否在职中)。
# 这里故意用元组而不是 dataclass：领域模型 DateRange 要到 R5 才定义，
# 底层的文本工具不能反过来依赖上层结构。
Period = tuple[str | None, str | None, bool]
        #含义：变量可以是字符串，也可以是`None`。

def is_current_marker(raw: str | None) -> bool:   #"是否是在职标记"函数
    """判断一个字符串是不是「在职中」的标记。 raw 是形参:传入**结束时间字段的原始文本**。

    '至今' / 'Present' / '现在'   → True
    '' / None / '今' / '2020-07'  → False

    空值明确返回 False：**没写结束时间 ≠ 干到今天**。
    把它当成在职中会让经验年限虚高，是参考项目"天文数字年限"bug 的近亲。
    """
    if not isinstance(raw, str):
        return False
    return raw.strip().lower() in _CURRENT_MARKERS  #检查处理后的字符串**是否存在于冻结集合的关键词里面**。


def _month_index(ym: str | None) -> int | None:
    """'YYYY-MM' → 从公元 0 年 1 月起算的月序号，用于整数加减。

    '2020-07' → 2020 * 12 + 7 = 24247
    '2021-07' → 24307            两者相减 = 12，正好一年

    这里复用 parse_year_month 而不是自己切字符串：日期解析只有一处实现，
    哪些脏输入该拒绝，也只在一个地方维护。
    """
    parsed = parse_year_month(ym)
    if parsed is None:
        return None
    year_text, month_text = parsed.split("-")
    return int(year_text) * 12 + int(month_text)


def months_between(start_ym: str | None, end_ym: str | None) -> int | None:
    """两个年月之间经历了多少个月（差值法）。

    months_between('2020-01', '2020-02')  → 1
    months_between('2025-08', '2026-09')  → 13
    months_between('2020-01', '2019-12')  → None   （倒挂，数据有误）

    任一端解析不了、或结束早于开始，都返回 None —— 交给上层决定怎么处理，
    而不是在这里返回 0 假装"零个月"。
    """
    start_idx = _month_index(start_ym)
    end_idx = _month_index(end_ym)
    if start_idx is None or end_idx is None:
        return None

    months = end_idx - start_idx
    return months if months >= 0 else None 
    #值1 if 条件 else 值2   条件成立返回值1 否则返回值2

def merge_experience_months(periods: Sequence[Period], today_ym: str) -> int:
    """多段工作经历的总月数，时间重叠的部分取并集。

    参数:
        periods:  若干 (开始年月, 结束年月, 是否在职中) 元组。
        today_ym: 计算"至今"长度时使用的当前年月，格式 'YYYY-MM'。

    today_ym 必须是参数，不能在函数内部调 datetime.now()：
      1) 否则测试结果会随时间漂移 —— 今天能过，明年就挂；
      2) 更严重的是，ARCHITECTURE.md §4.4 要求绝不把"写入时刻的时钟"固化进数据。

    边界处理:
        - 开始时间解析不了        → 跳过该段
        - 结束时间缺失且非在职    → 跳过该段（不猜）
        - 结束早于开始（倒挂）    → 跳过该段
        - 结束时间写成"至今"      → 即使在职标记没传对，也按在职中处理
        - 所有段都无效            → 返回 0
    """
    today_idx = _month_index(today_ym)
    if today_idx is None:
        # today_ym 由程序传入，格式错误属于编程错误，必须立刻炸出来。
        # 若悄悄当成 0 个月，一整批候选人的年限会静默归零 —— 这正是
        # ARCHITECTURE.md §6.3「规则 1」反对的那类"配置错误无声通过"。
        raise ValueError(f"today_ym 不是合法的年月：{today_ym!r}")

    # ① 把每段经历换算成 [开始月序号, 结束月序号]
    spans: list[tuple[int, int]] = []
    for start_ym, end_ym, is_current in periods:
        start_idx = _month_index(start_ym)
        if start_idx is None:
            continue

        if is_current or is_current_marker(end_ym):
            end_idx = today_idx
        else:
            end_idx = _month_index(end_ym)

        if end_idx is None or end_idx < start_idx:
            continue

        spans.append((start_idx, end_idx))

    if not spans:
        return 0

    # ② 按开始时间排序，从左到右合并重叠区间
    #
    #    [2020-01 ........ 2020-12]
    #              [2020-06 ........ 2021-06]
    #    合并后：[2020-01 .............. 2021-06]   —— 2020-06~12 只算一次
    spans.sort()
    merged_start, merged_end = spans[0]
    total = 0
    for start_idx, end_idx in spans[1:]:
        if start_idx <= merged_end:
            # 与本段有重叠（或正好相接）→ 延长当前段，不另起一段
            merged_end = max(merged_end, end_idx)
        else:
            total += merged_end - merged_start
            merged_start, merged_end = start_idx, end_idx

    total += merged_end - merged_start
    return total


# ============================================================
# 薪资解析
#
# 简历里写「20K-30K」「2万-3万」「面议」的都有，岗位需求那边也一样。
# 统一解析成「月薪区间（元）」，评分和硬性过滤共用同一个解析器 ——
# 两处各写一份，就会出现"评分认为是 20K、过滤认为是 2 万"的分裂。
# ============================================================

_SALARY_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def parse_salary_range(raw: str | None) -> tuple[int, int] | None:
    """把薪资写法解析成月薪区间，单位：元。

        '20K-30K'  '20-30k'  '20k'   → (20000, 30000) / (20000, 20000)
        '2万-3万'                     → (20000, 30000)
        '20000-30000'                 → (20000, 30000)
        '面议' / '' / None / '年薪30万' → None

    解析不了就返回 None，不猜。理由和 parse_year_month 完全一样：
    猜错的值不会报错，它会安静地流进评分和过滤里。
    """
    if not isinstance(raw, str):
        return None

    text = raw.strip().lower().replace("，", ",").replace(" ", "")
    if not text or "面议" in text or "negotiable" in text:
        return None

    # 年薪 / 月薪混用会引入一个隐式的「除以 12」，先不做。
    # 见到就返回 None，让上层当作"没有薪资数据"处理，比算错强。
    if "/年" in text or "年薪" in text or "/yr" in text:
        return None

    numbers = [float(n) for n in _SALARY_NUMBER.findall(text)]
    if not numbers:
        return None

    unit = 1
    if "万" in text or "w" in text:
        unit = 10000
    elif "k" in text or "千" in text:
        unit = 1000
    elif numbers[0] < 1000:
        # 裸数字「20-30」在国内简历里基本都指 K，按 K 理解。
        # 这个假设写在文档里，而不是藏在代码里。
        unit = 1000

    low = numbers[0] * unit
    high = numbers[1] * unit if len(numbers) >= 2 else low
    if low > high:
        low, high = high, low

    if low <= 0:
        return None
    return int(low), int(high)


def normalize_token(text: str) -> str:
    """归一化函数
    技能 / 行业 / 城市 / 标签比较前的统一写法：小写、去空格。

    'Python 3' 和 'python3' 归一后都是 'python3'。

    为什么放在 text.py 而不是各模块各写一行？
        三处要用它：评分比较（scoring）、拆表写入（sqlite_repo）、
        将来的 SQL 下推查询条件。三份复制品迟早有一份忘了改。
    """
    return text.strip().lower().replace(" ", "")
