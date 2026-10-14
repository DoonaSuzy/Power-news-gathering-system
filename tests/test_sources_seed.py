"""来源种子清单自检（``sources_seed.validate``）的回归测试。

## 测什么

``validate()`` 是 ``python -m newsagg validate`` 与 ``doctor`` 的判据，**长期无人
守**：三条硬编码守卫（基础清单 39 条 / ``csg`` 5 条 / ``province_code`` 非空）
在来源清单扩到 60 条后全部过期，命令天天 exit 1，真问题反而被噪声淹没。

曾经怎么坏的：

1. ``len(SOURCES) != 39`` —— 基础清单实际 40 条（``dbcec0e`` 起就是），写入即错；
2. ``expected = {"csg": 5, ...}`` —— ``csg`` 实际 6 条（含停用的 ``csg_hq``）；
3. ``if code is not None and code not in PROVINCE_BY_CODE`` —— 行业站/全国站用
   **空串**表示「无省份」，空串不是 ``None``，于是 ``csg_hq``` 被误报
   「province_code= 不在 34 省字典中」，而**真正的越界代码会被一起放过**。

所以这里既钉住「当前清单必须通过」，也钉住「越界 / 重复 / 条数漂移必须被抓到」。
"""

from __future__ import annotations

import copy

import pytest

from newsagg import sources_seed
from newsagg.sources_seed import ALL_SOURCES, SOURCE_BY_ID, SOURCES, validate


def _clone() -> list[dict]:
    return copy.deepcopy(list(ALL_SOURCES))


def _patch_all(monkeypatch: pytest.MonkeyPatch, items: list[dict]) -> None:
    """替换模块级 ALL_SOURCES（validate 在调用时按全局名取）。"""
    monkeypatch.setattr(sources_seed, "ALL_SOURCES", items)


class TestCurrentSeedPasses:
    """当前清单必须干净 —— 这条挂了就是清单本身坏了，不是守卫坏了。"""

    def test_validate_returns_no_problem(self):
        assert validate() == []

    def test_base_list_is_40_and_all_is_60(self):
        assert len(SOURCES) == 40
        assert len(ALL_SOURCES) == 60

    def test_ids_are_unique(self):
        assert len(SOURCE_BY_ID) == len(ALL_SOURCES)

    def test_csg_hq_is_kept_disabled_for_compliance(self):
        """robots.txt 全站 Disallow 的站**保留但不启用**，别当垃圾清掉。"""
        hq = SOURCE_BY_ID["csg_hq"]
        assert hq["enabled"] is False
        assert "robots" in hq["note"]


class TestProvinceCodeGuard:
    """第 2 项守卫：空串是「无省份」，只有越界代码才是问题。"""

    def test_empty_string_does_not_produce_false_positive(self, monkeypatch):
        items = _clone()
        assert any(s["province_code"] == "" for s in items)  # 前提：种子确实用空串
        _patch_all(monkeypatch, items)
        assert validate() == []

    def test_out_of_range_code_is_caught(self, monkeypatch):
        items = _clone()
        for s in items:
            if s["id"] == "csg_hq":
                s["province_code"] = "999999"  # 不存在于 34 省字典
        _patch_all(monkeypatch, items)
        assert any("csg_hq" in p and "999999" in p for p in validate())


class TestDriftGuards:
    """第 1、3 项守卫：条数与类别分布漂移必须报出来。"""

    def test_base_count_drift_is_caught(self, monkeypatch):
        monkeypatch.setattr(sources_seed, "SOURCES", SOURCES[:-1])
        assert any("原始来源总数应为 40" in p for p in validate())

    def test_category_drift_is_caught(self, monkeypatch):
        monkeypatch.setattr(
            sources_seed,
            "SOURCES",
            [s for s in SOURCES if s["id"] != "csg_hq"],
        )
        assert any("类别 csg 应有 6 条" in p for p in validate())

    def test_duplicate_id_is_caught(self, monkeypatch):
        items = _clone()
        items.append(copy.deepcopy(items[0]))
        _patch_all(monkeypatch, items)
        assert any("重复的来源 id" in p for p in validate())
