"""app/domain/policies.py 的单测：配置加载与校验。

重点测「错误配置必须报错」——这是本模块存在的理由。
如果一条非法配置能悄悄加载成功，R6 的评分就会用一个错权重跑完全程。
"""

from pathlib import Path

import pytest

from app.domain.policies import (
    ScoringConfig,
    UnknownFieldPolicy,
    load_scoring_config,
)

# 真实配置文件。用 __file__ 推路径，不依赖测试从哪个目录启动。
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"


def _write(tmp_path: Path, text: str) -> Path:
    """把一段 YAML 写进临时文件，供错误场景使用。"""
    path = tmp_path / "scoring.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ============================================================
# 正常路径：真实配置能加载
# ============================================================

def test_real_config_loads():
    config = load_scoring_config(CONFIG_PATH)
    assert isinstance(config, ScoringConfig)
    assert config.version == 1
    assert config.default_profile == "tech"
    assert sorted(config.profiles) == ["sales", "strict", "tech"]


def test_tech_profile_weights_sum_to_one():
    tech = load_scoring_config(CONFIG_PATH).profile("tech")
    assert tech.dim_weight_total == pytest.approx(1.0)
    assert tech.dims["skill"].weight == pytest.approx(0.40)
    assert tech.semantic.weight == pytest.approx(0.20)
    assert tech.recall.top_k == 100
    assert tech.unknown_field_policy is UnknownFieldPolicy.KEEP


def test_profile_defaults_to_default_profile():
    config = load_scoring_config(CONFIG_PATH)
    assert config.profile() is config.profile("tech")


def test_inherits_takes_parent_and_overrides_only_listed_keys():
    """strict 继承 tech：dims / semantic 来自父档案，recall 逐键合并。"""
    strict = load_scoring_config(CONFIG_PATH).profile("strict")
    tech = load_scoring_config(CONFIG_PATH).profile("tech")

    assert strict.dims == tech.dims           # 没有写 dims → 完全继承
    assert strict.semantic == tech.semantic
    assert strict.recall.top_k == 200         # 子档案覆盖
    assert strict.recall.min_similarity == pytest.approx(0.40)
    assert strict.unknown_field_policy is UnknownFieldPolicy.REJECT


def test_unknown_profile_raises_keyerror():
    with pytest.raises(KeyError, match="未知档案"):
        load_scoring_config(CONFIG_PATH).profile("devops")


# ============================================================
# 错误路径：每条都必须失败，且错误信息里能看出实际值
# ============================================================

BASE = """
version: 1
default_profile: p
profiles:
  p:
    dims:
      skill: { weight: 0.5 }
      industry: { weight: 0.5 }
    semantic: { weight: 0.2 }
"""


def test_weights_not_summing_to_one_is_rejected(tmp_path):
    """六维和 0.8 —— 必须报错，且错误信息带出实际值。"""
    path = _write(tmp_path, BASE.replace(
        "industry: { weight: 0.5 }", "industry: { weight: 0.3 }"
    ))
    with pytest.raises(ValueError, match=r"0\.800000"):
        load_scoring_config(path)


def test_typo_in_dim_name_is_rejected(tmp_path):
    """skill 拼成 skll：名字不认识，必须报错而不是当成第七个维度。"""
    path = _write(tmp_path, BASE.replace("skill:", "skll:"))
    with pytest.raises(ValueError, match="未知维度"):
        load_scoring_config(path)


def test_typo_in_field_name_is_rejected(tmp_path):
    """weight 拼成 weigth：extra='forbid' 会拦住它。"""
    path = _write(tmp_path, BASE.replace("weight: 0.5", "weigth: 0.5", 1))
    with pytest.raises(ValueError):
        load_scoring_config(path)


def test_negative_weight_is_rejected(tmp_path):
    path = _write(tmp_path, BASE.replace("weight: 0.5", "weight: -0.5", 1))
    with pytest.raises(ValueError):
        load_scoring_config(path)


def test_semantic_weight_out_of_range_is_rejected(tmp_path):
    path = _write(tmp_path, BASE.replace("semantic: { weight: 0.2 }", "semantic: { weight: 1.5 }"))
    with pytest.raises(ValueError):
        load_scoring_config(path)


def test_inherits_cycle_is_rejected(tmp_path):
    """a → b → a 的循环继承必须报错，不能让递归无限下去。"""
    path = _write(tmp_path, """
version: 1
default_profile: a
profiles:
  a:
    inherits: b
  b:
    inherits: a
""")
    with pytest.raises(ValueError, match="循环"):
        load_scoring_config(path)


def test_inherits_missing_parent_is_rejected(tmp_path):
    path = _write(tmp_path, BASE + "    inherits: nope\n")
    with pytest.raises(ValueError, match="不存在的档案"):
        load_scoring_config(path)


def test_default_profile_missing_from_profiles_is_rejected(tmp_path):
    path = _write(tmp_path, BASE.replace("default_profile: p", "default_profile: q"))
    with pytest.raises(ValueError, match="default_profile"):
        load_scoring_config(path)


def test_empty_profiles_is_rejected(tmp_path):
    path = _write(tmp_path, "version: 1\ndefault_profile: p\nprofiles: {}\n")
    with pytest.raises(ValueError, match="profiles"):
        load_scoring_config(path)


def test_missing_default_profile_key_is_rejected(tmp_path):
    path = _write(tmp_path, BASE.replace("default_profile: p\n", ""))
    with pytest.raises(ValueError, match="default_profile"):
        load_scoring_config(path)


def test_top_level_not_a_mapping_is_rejected(tmp_path):
    path = _write(tmp_path, "- a\n- b\n")
    with pytest.raises(ValueError, match="顶层"):
        load_scoring_config(path)
