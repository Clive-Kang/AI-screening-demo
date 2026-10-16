"""冒烟测试：只验证脚手架是否搭通，不涉及任何业务逻辑。

名词解释 —— "冒烟测试"（smoke test）
    来自硬件行业：新板子通电后先看它冒不冒烟。不冒烟，才值得花时间做详细测试。
    软件里的含义相同：用最粗粒度的几个断言，先证明"系统还能起来"。

本文件存在的意义
    R1 这一轮没有业务代码可测，但必须有一个东西能证明：
    "在项目根目录敲 pytest 时，Python 找得到 app 包"。
    这个前提若不成立，后面每一轮的测试都会以一个看不懂的 ImportError 收场。
    所以先把它钉死。
"""

import pydantic
import yaml

import app
import app.domain


def test_package_importable():
    """app 与 app.domain 都能被导入 —— 证明包搜索路径配置正确。

    如果这条失败（报 ModuleNotFoundError: No module named 'app'），
    原因几乎总是"没在项目根目录下执行命令"，而不是代码写错了。
    详见 CLAUDE.md §4 的坑 #1。
    """
    assert app.__version__, "app 包应带版本号"
    assert app.domain is not None, "app.domain 子包应可导入"


def test_stage1_dependencies_available():
    """阶段 1 的两个第三方依赖确实可用，且版本符合要求。

    提前钉死版本，好过等到 R4 写配置校验时才发现环境里是 pydantic v1——
    v1 和 v2 的 API 完全不兼容，那时再排查会绕远路。
    """
    major = int(pydantic.VERSION.split(".")[0])
    assert major >= 2, f"需要 pydantic v2，当前为 {pydantic.VERSION}"

    # PyYAML 的最小可用性验证：能把 YAML 文本解析成 Python 字典
    assert yaml.safe_load("weight: 0.4") == {"weight": 0.4}
