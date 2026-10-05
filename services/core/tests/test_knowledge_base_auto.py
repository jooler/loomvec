"""知识库系统新建的名称查重 + 系统入库资产的管理权限规则（纯函数）。"""

from __future__ import annotations

import uuid

import pytest

from loomvec.core.authz import can_manage_asset, is_system_ingested
from loomvec.core.constants import ASSET_META_SOURCE_KEY, ASSET_SOURCE_INGEST_WATCH
from loomvec.core.db.models import Tag, TagStatus
from loomvec.core.knowledge_base_auto import find_same_name_tag, name_key


def _tags(*names: str, status: str = TagStatus.ACTIVE.value) -> list[Tag]:
    return [Tag(name=n, status=status) for n in names]


@pytest.mark.parametrize(
    ("existing", "suggested"),
    [
        ("Deep Learning", "deep-learning"),
        ("Deep Learning", "  DEEP   learning "),
        ("Graph Neural Networks", "graph neural network"),
        ("LLMs", "LLM"),
        ("肿瘤", "肿瘤 "),
        ("单细胞 测序", "单细胞测序"),
        ("Mass Spectrometry", "mass_spectrometry"),
    ],
)
def test_spelling_variants_reuse_existing(existing, suggested):
    assert find_same_name_tag(_tags(existing), suggested).name == existing


@pytest.mark.parametrize(
    ("existing", "suggested"),
    [
        # 审查复现的误并：近似但语义不同 / 相反的兄弟主题
        ("1型糖尿病", "2型糖尿病"),
        ("HER2阴性乳腺癌", "HER2阳性乳腺癌"),
        ("小细胞肺癌", "非小细胞肺癌"),
        ("BRCA1", "BRCA2"),
        ("CD4+ T细胞", "CD8+ T细胞"),
        ("DNA甲基化", "RNA甲基化"),
        ("Type 1 Diabetes", "Type 2 Diabetes"),
        ("神经网络", "图神经网络"),
        ("肿瘤", "肿瘤免疫"),
        ("Class", "Clas"),
    ],
)
def test_near_but_different_names_are_not_merged(existing, suggested):
    assert find_same_name_tag(_tags(existing), suggested) is None


def test_symbol_only_name_never_matches():
    assert name_key("!!! ---") == ""
    assert find_same_name_tag(_tags("---"), "!!!") is None
    assert name_key("-") == ""
    assert name_key("+") == ""
    assert name_key("#") == ""
    assert name_key("!!! +++") == ""


@pytest.mark.parametrize(
    ("existing", "suggested"),
    [
        # 三轮审查复现：生物医学正负号后缀写法，语义相反，不能误并
        ("HER2+ 乳腺癌", "HER2- 乳腺癌"),
        ("ER+", "ER-"),
        ("C", "C++"),
        ("CD4+ T细胞", "CD4- T细胞"),
        ("PD-L1+", "PD-L1-"),
        ("HER2+/-", "HER2+"),
        ("HER2(-)", "HER2"),
        ("ER -", "ER"),
        # Unicode 书写变体：上标、全角、数学减号、LaTeX 写法
        ("CD4⁺ T细胞", "CD4⁻ T细胞"),
        ("CD4⁺ T细胞", "CD4 T细胞"),
        ("CD4＋", "CD4－"),
        ("HER2− 乳腺癌", "HER2 乳腺癌"),
        ("HER2（-）", "HER2"),
        ("$CD4^{-}$ T细胞", "CD4 T细胞"),
    ],
)
def test_sign_suffix_variants_are_not_merged(existing, suggested):
    assert find_same_name_tag(_tags(existing), suggested) is None


@pytest.mark.parametrize(
    ("existing", "suggested"),
    [
        ("COVID-19", "covid19"),
        ("IL-6", "IL6"),
        ("pre-mRNA", "premRNA"),
        ("1,2-二甲基苯", "1,2二甲基苯"),
    ],
)
def test_connector_hyphen_variants_still_merge(existing, suggested):
    assert find_same_name_tag(_tags(existing), suggested).name == existing


def test_active_preferred_over_pending():
    pending = _tags("Machine Learning", status=TagStatus.PENDING.value)
    active = _tags("machine-learning")
    assert find_same_name_tag(pending + active, "Machine Learning").status == TagStatus.ACTIVE


ALICE = uuid.uuid4()
SYSTEM_META = {ASSET_META_SOURCE_KEY: ASSET_SOURCE_INGEST_WATCH}


def test_is_system_ingested():
    assert is_system_ingested(SYSTEM_META)
    assert not is_system_ingested({})
    assert not is_system_ingested(None)
    assert not is_system_ingested({ASSET_META_SOURCE_KEY: "upload"})


def test_system_ingested_asset_manage_rules():
    def can(role, user_id, created_by=None):
        return can_manage_asset(
            role=role, asset_created_by=created_by, user_id=user_id, system_ingested=True
        )

    assert can("owner", ALICE)
    assert can("editor", ALICE)  # 人类 editor 可管（无上传人，视作空间共有）
    assert not can("editor", None)  # API Key 不落入「API Key 资产池」
    assert not can("viewer", ALICE)
    # 有上传人时按普通规则（如副本归复制者）
    other = uuid.uuid4()
    assert can("editor", ALICE, created_by=ALICE)
    assert not can("editor", ALICE, created_by=other)
    # 未标记的无主资产仍按原 API Key 资产池语义
    assert can_manage_asset(role="editor", asset_created_by=None, user_id=None)
    assert not can_manage_asset(role="editor", asset_created_by=None, user_id=ALICE)
