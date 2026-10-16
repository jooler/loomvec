"""抽取领域配置（graph/profiles）：prompt 领域插槽、分层噪音过滤、后置章节屏蔽开关、
类型白名单、空间覆盖解析、自定义配置校验、缓存键与指纹。纯函数级，不经 DB/LLM。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from loomvec.core.config import Settings
from loomvec.core.constants import DEFAULT_EXTRACTION_PROFILE
from loomvec.core.db.models import Space, SpaceType
from loomvec.core.graph.entity_filter import is_blocked_relation, noise_reason
from loomvec.core.graph.ontology import canonical_type, entity_key, normalize_entity_name
from loomvec.core.graph.profiles import (
    ACADEMIC,
    BIOMEDICAL,
    BUILTIN_PROFILES,
    GENERAL,
    LEGAL,
    ExtractionProfile,
    available_profiles,
    parse_custom_profiles,
)
from loomvec.core.pipeline.base import chunk_cache_key, resolve_extraction_profile
from loomvec.core.pipeline.extraction import build_extraction_messages, parse_merged
from loomvec.core.pipeline.sections import is_skip_heading

DOC = [
    "# 标题",  # 1
    "## 正文",  # 2
    "甲公司与乙公司签订供货合同。",  # 3
    "## References",  # 4
    "1. Smith J. A big paper title. Nature 2020.",  # 5
    "## Additional information",  # 6
    "保修期两年。",  # 7
]


def _system_prompt(profile: ExtractionProfile, lines: list[str] = DOC) -> tuple[str, str]:
    msgs = build_extraction_messages(
        lines,
        1,
        len(lines),
        min_chars=100,
        max_chars=1000,
        max_line_chars=500,
        profile=profile,
    )
    return msgs[0]["content"], msgs[1]["content"]


# ---------------------------------------------------------------------------
# prompt：底座领域无关，领域内容来自配置
# ---------------------------------------------------------------------------


def test_general_prompt_has_no_biomedical_or_paper_wording():
    system, user = _system_prompt(GENERAL)
    for word in (
        "gene_protein",
        "cell_type",
        "compound",
        "基因",
        "蛋白",
        "抗体",
        "质粒",
        "试剂",
        "TGFB1",
        "EMT",
        "论文",
        "[略]",
    ):
        assert word not in system, word
    # 不屏蔽后置章节：References / Additional information 原文照送
    assert "L5: 1. Smith J." in user and "L7: 保修期两年。" in user
    assert "锚点实体" in system and "跨文档" in system and "aliases" in system
    assert "本批最多 5 个实体" in system


def test_biomedical_prompt_keeps_all_v4_domain_points():
    system, user = _system_prompt(BIOMEDICAL)
    for t in (
        "person",
        "organization",
        "product",
        "location",
        "event",
        "concept",
        "gene_protein",
        "disease",
        "cell_type",
        "compound",
        "method",
        "dataset",
        "other",
    ):
        assert f"{t}=" in system, t
    # 优先抽 / 不要抽
    assert "基因/蛋白 gene_protein、疾病/表型 disease、细胞类型/组织 cell_type" in system
    assert "作者、单位、基金、厂商一律不抽" in system
    for phrase in (
        "cells、patients、mice",
        "样本/患者/批次编号",
        "数据库登录号",
        "通用统计检验",
        "抗体、试剂、耗材、质粒及其货号",
        "细胞培养/转染/文库构建等实验操作步骤",
        "标注 [略] 的行里的任何内容",
    ):
        assert phrase in system, phrase
    # v4 关系集合与行政性关系排除
    for rel in (
        "USES",
        "PART_OF",
        "IS_A",
        "REGULATES",
        "ACTIVATES",
        "INHIBITS",
        "INTERACTS_WITH",
        "TARGETS",
        "CAUSES",
        "TREATS",
        "EXPRESSED_IN",
        "ASSOCIATED_WITH",
        "DERIVED_FROM",
        "TRAINED_ON",
        "EVALUATED_ON",
        "COMPARED_WITH",
        "OUTPERFORMS",
        "LOCATED_IN",
    ):
        assert rel in system, rel
    for rel in ("AFFILIATED_WITH", "FUNDED_BY", "SUPPLIED_BY", "PUBLISHED_IN"):
        assert rel in system.split("不要抽 ")[-1], rel
    # EMT/TGFB1 示例与基因符号规则
    assert '"name": "Epithelial-to-mesenchymal transition"' in system
    assert '"aliases": ["EMT"]' in system
    assert '"head": "TGFB1"' in system and '"type": "REGULATES"' in system
    assert "基因符号保持原文大小写" in system
    # 后置章节屏蔽：References 起到下一个非跳过标题前都是 [略]
    assert "L5: [略]" in user and "Smith" not in user
    assert "L7: [略]" in user


def test_legal_prompt_uses_legal_slots_without_back_matter_masking():
    system, user = _system_prompt(LEGAL)
    for t in ("statute=", "case=", "cause=", "contract="):
        assert t in system
    for rel in ("CITES", "APPLIES", "EMPLOYED_BY", "SUPPLIED_BY", "PLAINTIFF_IN"):
        assert rel in system
    assert "不要抽 " not in system  # 无拦截集合
    assert "[略]" not in system and "[略]" not in user


# ---------------------------------------------------------------------------
# 兜底过滤：通用层 + 论文元数据层
# ---------------------------------------------------------------------------


def test_legal_keeps_staff_and_supplier_entities_and_relations():
    assert noise_reason("张三", "person", "被告公司的员工", profile=LEGAL) is None
    assert noise_reason("李四", "person", "公司创始人，本案第三人", profile=LEGAL) is None
    assert noise_reason("丙公司", "organization", "原告的设备供应商", profile=LEGAL) is None
    for rel in ("EMPLOYED_BY", "SUPPLIED_BY", "CITES"):
        assert not is_blocked_relation(rel, profile=LEGAL)
    merged = parse_merged(
        {
            "chunk_markers": [],
            "entities": [
                {"name": "张三", "type": "person", "description": "被告公司的员工"},
                {"name": "丙公司", "type": "organization", "description": "设备供应商"},
                {"name": "民法典第五百七十七条", "type": "statute", "description": "违约责任条款"},
            ],
            "relations": [
                {"head": "张三", "tail": "丙公司", "type": "EMPLOYED_BY"},
                {"head": "丙公司", "tail": "民法典第五百七十七条", "type": "CITES"},
            ],
        },
        batch_start=1,
        batch_end=3,
        profile=LEGAL,
    )
    assert [e.type for e in merged.entities] == ["person", "organization", "statute"]
    assert [r.type for r in merged.relations] == ["EMPLOYED_BY", "CITES"]


@pytest.mark.parametrize("profile", [ACADEMIC, BIOMEDICAL])
def test_paper_profiles_keep_v4_noise_filtering(profile: ExtractionProfile):
    assert noise_reason("张三", "person", "公司员工", profile=profile) == "person_author_or_staff"
    assert noise_reason("Zoe Wefers", "person", "论文作者，斯坦福大学", profile=profile)
    assert (
        noise_reason("Thermo Fisher", "organization", "试剂供应商", profile=profile)
        == "org_affiliation_funder_vendor"
    )
    assert noise_reason("Cell", "other", "被引期刊", profile=profile) == "reference_or_section"
    for rel in ("AFFILIATED_WITH", "FUNDED_BY", "CITES", "EMPLOYED_BY"):
        assert is_blocked_relation(rel, profile=profile)


def test_vendor_relations_blocked_only_for_biomedical():
    for rel in ("SUPPLIED_BY", "MANUFACTURED_BY", "PROVIDED_BY", "PRODUCED_BY"):
        assert is_blocked_relation(rel, profile=BIOMEDICAL)
        assert not is_blocked_relation(rel, profile=ACADEMIC)
    # biomedical 的拦截集合 = v4 的 BLOCKED_RELATION_TYPES
    assert BIOMEDICAL.blocked_set == frozenset(
        {
            "AFFILIATED_WITH",
            "FUNDED_BY",
            "SUPPLIED_BY",
            "MANUFACTURED_BY",
            "PROVIDED_BY",
            "PRODUCED_BY",
            "PUBLISHED_IN",
            "AUTHORED_BY",
            "AUTHOR_OF",
            "CITED_BY",
            "CITES",
            "SPONSORED_BY",
            "EMPLOYED_BY",
            "WORKS_AT",
        }
    )


def test_general_rules_apply_to_every_profile():
    for profile in (GENERAL, LEGAL):
        assert noise_reason("x" * 130, "other", None, profile=profile) == "name_too_long"
        assert noise_reason("Perez et al.", "person", "", profile=profile)
        assert noise_reason("王五", "person", "本文通讯作者", profile=profile)
        assert noise_reason("某大学", "organization", "作者单位之一", profile=profile)
        assert noise_reason("Online Methods", "other", "Section heading.", profile=profile)
        assert noise_reason("A survey", "work", "本文参考文献中的综述", profile=profile)
        # 通用层不按「作者/员工/供应商/被引」词表误杀
        assert (
            noise_reason("曹雪芹", "person", "清代小说家，《红楼梦》作者", profile=profile) is None
        )
        assert noise_reason("丁公司", "organization", "零部件供应商", profile=profile) is None
        assert noise_reason("某判例", "other", "被引用的判例", profile=profile) is None
    assert not GENERAL.blocked_relations


# ---------------------------------------------------------------------------
# 后置章节：Declaration 收窄
# ---------------------------------------------------------------------------


def test_declaration_heading_narrowed_to_interest_and_ai_statements():
    assert not is_skip_heading("## Declarations")
    assert not is_skip_heading("## Declaration")
    assert not is_skip_heading("## Declaration of conformity")
    assert is_skip_heading("## Declaration of interests")
    assert is_skip_heading("## Declaration of Competing Interest")
    assert is_skip_heading("## Declarations of interest")
    assert is_skip_heading("## Declaration of generative AI and AI-assisted technologies")
    # BMC 式 Declarations 下的子标题各自命中
    assert is_skip_heading("### Ethics approval and consent to participate")
    assert is_skip_heading("### Consent for publication")
    assert is_skip_heading("### Availability of data and materials")
    assert is_skip_heading("### Competing interests")


# ---------------------------------------------------------------------------
# 类型：领域无关的规范化 + 配置白名单
# ---------------------------------------------------------------------------

_V4_TYPES = (
    "person",
    "organization",
    "product",
    "location",
    "event",
    "concept",
    "gene_protein",
    "disease",
    "cell_type",
    "compound",
    "method",
    "dataset",
    "other",
)


def test_canonical_type_keeps_existing_entity_keys():
    for t in _V4_TYPES:
        assert canonical_type(t) == t
        assert canonical_type(f" {t.upper()} ") == t
        expected = hashlib.sha256(f"sp|{normalize_entity_name('TP53')}|{t}".encode()).hexdigest()
        assert entity_key("sp", "TP53", t) == expected
    assert canonical_type("statute") == "statute"
    for bad in (None, "", "gene-protein", "1abc", "基因", "x" * 33):
        assert canonical_type(bad) == "other"


def test_parse_coerces_types_outside_profile_whitelist():
    raw = {
        "chunk_markers": [],
        "entities": [
            {"name": "A", "type": "statute"},
            {"name": "B", "type": "gene_protein"},
            {"name": "C", "type": "work"},
        ],
        "relations": [],
    }

    def types(profile: ExtractionProfile) -> list[str]:
        merged = parse_merged(raw, batch_start=1, batch_end=1, profile=profile)
        return [e.type for e in merged.entities]

    assert types(LEGAL) == ["statute", "other", "other"]
    assert types(BIOMEDICAL) == ["other", "gene_protein", "other"]
    assert types(GENERAL) == ["other", "other", "work"]


# ---------------------------------------------------------------------------
# 解析优先级：本次运行覆盖 > 空间 > 全局；不可用的值回落
# ---------------------------------------------------------------------------


def _settings(global_key: str) -> Settings:
    # 只读 pipeline.extraction_profile：用简易对象，避免改动共享的嵌套配置实例
    return SimpleNamespace(pipeline=SimpleNamespace(extraction_profile=global_key))  # type: ignore[return-value]


def _space(value: str | None) -> Space:
    return Space(name="x", slug="x", space_type=SpaceType.PERSONAL, extraction_profile=value)


def test_resolve_extraction_profile_priority_and_fallback():
    profiles = available_profiles(None)
    s = _settings("academic")
    assert resolve_extraction_profile(s, None, profiles).key == "academic"
    assert resolve_extraction_profile(s, _space(None), profiles).key == "academic"
    assert resolve_extraction_profile(s, _space("legal"), profiles).key == "legal"
    assert resolve_extraction_profile(s, _space("legal"), profiles, "biomedical").key == (
        "biomedical"
    )
    # 空间引用的配置已不可用（如自定义配置被删）→ 回落全局
    assert resolve_extraction_profile(s, _space("gone"), profiles).key == "academic"
    # 全局也不可用 → 内置 general
    assert resolve_extraction_profile(_settings("gone"), None, profiles).key == "general"
    assert DEFAULT_EXTRACTION_PROFILE == "general" and "general" in BUILTIN_PROFILES


def test_resolve_extraction_profile_fallback_logs_warning():
    profiles = available_profiles(None)
    # 全局值也不可用 → 回落 general 并告警，带上各层取值便于排查
    with capture_logs() as logs:
        resolved = resolve_extraction_profile(_settings("gone"), _space("also_gone"), profiles)
    assert resolved.key == "general"
    warnings = [x for x in logs if x["event"] == "extraction_profile_fallback"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["space_value"] == "also_gone" and warnings[0]["global_value"] == "gone"
    # 正常解析（含空间引用的配置不可用而回落到有效的全局配置）不告警
    with capture_logs() as logs:
        assert resolve_extraction_profile(_settings("academic"), _space("gone"), profiles).key == (
            "academic"
        )
        assert resolve_extraction_profile(_settings("gone"), None, profiles, "legal").key == "legal"
    assert not [x for x in logs if x["event"] == "extraction_profile_fallback"]


def test_global_default_is_general():
    assert Settings(_env_file=None).pipeline.extraction_profile == "general"


# ---------------------------------------------------------------------------
# 自定义配置校验
# ---------------------------------------------------------------------------


def _custom_spec(**overrides) -> dict:
    spec = {
        "label": "专利文献",
        "description": "专利说明书与权利要求书",
        "entity_types": [
            {"type": "patent", "label": "专利"},
            {"type": "organization", "label": "申请人/机构"},
            {"type": "other", "label": "其他"},
        ],
        "priority_hint": "专利 patent 与申请人 organization",
        "exclude_hint": "审查员署名",
        "relation_types": ["FILED_BY", "CITES"],
        "blocked_relations": ["PUBLISHED_IN"],
        "name_rules": "专利优先用公开号",
        "example": {
            "entities": [
                {"name": "CN123456A", "type": "patent", "description": "一件发明专利"},
                {"name": "甲公司", "type": "organization", "description": "该专利的申请人"},
            ],
            "relation": {"head": "CN123456A", "tail": "甲公司", "type": "FILED_BY"},
        },
    }
    spec.update(overrides)
    return spec


def _profile_to_spec(p: ExtractionProfile) -> dict:
    return {
        "label": p.label,
        "description": p.description,
        "entity_types": [{"type": t, "label": label} for t, label in p.entity_types],
        "priority_hint": p.priority_hint,
        "exclude_hint": p.exclude_hint,
        "relation_types": list(p.relation_types),
        "blocked_relations": list(p.blocked_relations),
        "name_rules": p.name_rules,
        "example": p.example,
        "skip_back_matter": p.skip_back_matter,
        "academic_noise": p.academic_noise,
    }


def test_custom_profile_parsed_and_listed_with_builtins():
    profiles = available_profiles({"patent": _custom_spec()})
    assert set(BUILTIN_PROFILES) < set(profiles)
    p = profiles["patent"]
    assert not p.builtin and p.type_keys == ("patent", "organization", "other")
    assert not p.skip_back_matter and not p.academic_noise
    system, _ = _system_prompt(p)
    assert "patent=专利" in system and "专利优先用公开号" in system and "PUBLISHED_IN" in system


def test_builtin_profiles_satisfy_custom_profile_constraints():
    for key, p in BUILTIN_PROFILES.items():
        parsed = parse_custom_profiles({f"copy_{key}": _profile_to_spec(p)})[f"copy_{key}"]
        assert parsed.fingerprint == p.fingerprint


def _example(**overrides) -> dict:
    """基于合法示例改出一份（entities/relation 整体替换）。"""
    ex = {
        "entities": [
            {"name": "CN123456A", "type": "patent", "description": "一件发明专利"},
            {"name": "甲公司", "type": "organization", "description": "该专利的申请人"},
        ],
        "relation": {"head": "CN123456A", "tail": "甲公司", "type": "FILED_BY"},
    }
    ex.update(overrides)
    return ex


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ([], r"^自定义抽取配置须为 JSON 对象"),
        ({"general": _custom_spec()}, r"^配置 key 不能覆盖内置配置：general$"),
        ({"Bad-Key": _custom_spec()}, r"^配置 key 须为小写字母开头.*'Bad-Key'$"),
        (
            {"patent": _custom_spec(entity_types=[{"type": "Patent", "label": "专利"}])},
            r"^patent\.entity_types\.0\.type: String should match pattern",
        ),
        (
            {"patent": _custom_spec(entity_types=[{"type": "patent", "label": "专利"}])},
            r"^patent: entity_types 须包含兜底类型 other$",
        ),
        (
            {"patent": _custom_spec(relation_types=["filed by"])},
            r"^patent\.relation_types: 关系类型须为大写字母开头.*'filed by'$",
        ),
        (
            {"patent": _custom_spec(blocked_relations=["CITES"])},
            r"^patent: 关系不能同时在推荐集合与拦截集合中：\['CITES'\]$",
        ),
        ({"patent": _custom_spec(unknown=1)}, r"^patent\.unknown: Extra inputs are not permitted"),
        ({"patent": _custom_spec(label="")}, r"^patent\.label: String should have at least 1"),
        (
            {"patent": _custom_spec(priority_hint="x" * 601)},
            r"^patent\.priority_hint: String should have at most 600",
        ),
    ],
)
def test_custom_profile_validation_rejects(value, needle: str):
    with pytest.raises(ValueError, match=needle):
        parse_custom_profiles(value)


@pytest.mark.parametrize(
    ("example", "needle"),
    [
        # 关系头/尾不在示例实体里：模型照抄会产出被静默丢弃的关系
        (
            _example(relation={"head": "CN123456A", "tail": "乙公司", "type": "FILED_BY"}),
            r"^patent\.example: example\.relation\.tail 须是 .*'乙公司'$",
        ),
        (
            _example(relation={"head": "丙公司", "tail": "甲公司", "type": "FILED_BY"}),
            r"^patent\.example: example\.relation\.head 须是 .*'丙公司'$",
        ),
        (
            _example(relation={"head": "甲公司", "tail": "甲公司", "type": "FILED_BY"}),
            r"^patent\.example: example\.relation 的 head 与 tail 不能是同一个实体$",
        ),
        # 旧结构（单个 entity）不再接受：既缺 entities 又多出 entity
        (
            {
                "entity": {"name": "CN123456A", "type": "patent", "description": "d"},
                "relation": {"head": "CN123456A", "tail": "甲公司", "type": "FILED_BY"},
            },
            r"^patent\.example\.entities: Field required$",
        ),
        # 至少 2 个实体，且 name 不重复
        (
            _example(entities=[{"name": "CN123456A", "type": "patent", "description": "d"}]),
            r"^patent\.example\.entities: List should have at least 2 items",
        ),
        (
            _example(
                entities=[
                    {"name": "甲公司", "type": "organization", "description": "d"},
                    {"name": "甲公司", "type": "patent", "description": "d"},
                ]
            ),
            r"^patent\.example: example\.entities 中的 name 不能重复$",
        ),
        # 示例实体类型须在 entity_types 内；关系类型须在推荐集合内、且不在拦截集合内
        (
            _example(
                entities=[
                    {"name": "CN123456A", "type": "person", "description": "d"},
                    {"name": "甲公司", "type": "organization", "description": "d"},
                ]
            ),
            r"^patent: example\.entities 的 type 须是 entity_types 中的类型：'person'$",
        ),
        (
            _example(relation={"head": "CN123456A", "tail": "甲公司", "type": "OWNS"}),
            r"^patent: example\.relation\.type 须是 relation_types 中的关系：'OWNS'$",
        ),
    ],
)
def test_custom_profile_example_must_be_self_consistent(example: dict, needle: str):
    with pytest.raises(ValueError, match=needle):
        parse_custom_profiles({"patent": _custom_spec(example=example)})


def test_custom_profile_example_relation_cannot_be_a_blocked_type():
    # 示例关系落在 blocked 集合（不在推荐集合）：与「示例关系须在 relation_types」一起，
    # 保证示例不会教模型输出被确定性拦截的关系
    spec = _custom_spec(blocked_relations=["PUBLISHED_IN", "OWNS"])
    spec["example"]["relation"]["type"] = "OWNS"
    with pytest.raises(ValueError, match=r"^patent: example\.relation\.type 须是 relation_types"):
        parse_custom_profiles({"patent": spec})


@pytest.mark.parametrize("profile", list(BUILTIN_PROFILES.values()), ids=lambda p: p.key)
def test_builtin_examples_are_self_consistent(profile: ExtractionProfile):
    ex = profile.example
    names = [e["name"] for e in ex["entities"]]
    assert len(ex["entities"]) >= 2 and len(set(names)) == len(names)
    rel = ex["relation"]
    assert rel["head"] in names and rel["tail"] in names and rel["head"] != rel["tail"]
    assert rel["type"] in profile.relation_types and rel["type"] not in profile.blocked_set
    assert all(e["type"] in profile.type_keys for e in ex["entities"])
    # prompt 渲染出的示例：每个示例实体都在 entities 里，关系头尾引用的就是这些 name
    system, _ = _system_prompt(profile)
    for e in ex["entities"]:
        assert f'"name": "{e["name"]}"' in system
    assert f'"head": "{rel["head"]}"' in system and f'"tail": "{rel["tail"]}"' in system


def test_empty_custom_value_means_builtins_only():
    assert parse_custom_profiles(None) == {} and parse_custom_profiles({}) == {}
    assert available_profiles(None).keys() == BUILTIN_PROFILES.keys()


# ---------------------------------------------------------------------------
# 指纹与缓存键
# ---------------------------------------------------------------------------


def test_fingerprint_tracks_extraction_content_only():
    assert BIOMEDICAL.fingerprint == replace(BIOMEDICAL).fingerprint
    assert replace(BIOMEDICAL, description="改了说明").fingerprint == BIOMEDICAL.fingerprint
    assert replace(BIOMEDICAL, exclude_hint="改了").fingerprint != BIOMEDICAL.fingerprint
    assert len({p.fingerprint for p in BUILTIN_PROFILES.values()}) == len(BUILTIN_PROFILES)


def test_chunk_cache_key_includes_profile_key_and_fingerprint():
    settings = Settings(_env_file=None)

    def key(profile: ExtractionProfile | None) -> str:
        return chunk_cache_key(settings, "sum", "m", extraction_profile=profile)

    assert key(BIOMEDICAL) != key(ACADEMIC) != key(None)
    assert key(BIOMEDICAL) == key(replace(BIOMEDICAL))
    assert key(replace(BIOMEDICAL, priority_hint="改了")) != key(BIOMEDICAL)


def test_chunk_cache_key_hits_only_for_identical_inputs():
    settings = Settings(_env_file=None)

    def key(**kw) -> str:
        base = {
            "checksum": "sum",
            "llm_model": "m",
            "chunk_strategy": "llm",
            "graph_extraction": True,
            "extraction_profile": BIOMEDICAL,
        }
        base.update(kw)
        return chunk_cache_key(
            settings,
            base["checksum"],
            base["llm_model"],
            chunk_strategy=base["chunk_strategy"],
            graph_extraction=base["graph_extraction"],
            extraction_profile=base["extraction_profile"],
        )

    assert key() == key()
    # 同 key、同内容的另一个实例（如自定义配置重新从库里解析出来）：指纹一致 → 命中
    assert key() == key(extraction_profile=replace(BIOMEDICAL))
    # 换配置 key（内容不变）→ 不命中
    assert key(extraction_profile=replace(BIOMEDICAL, key="bio_copy")) != key()
    # 同一 key、内容变了（指纹变）→ 不命中；改 example 同样算内容
    assert key(extraction_profile=replace(BIOMEDICAL, name_rules="改了")) != key()
    changed_example = {**BIOMEDICAL.example, "entities": BIOMEDICAL.example["entities"][::-1]}
    assert key(extraction_profile=replace(BIOMEDICAL, example=changed_example)) != key()
    # 只改不影响抽取的字段（description）→ 仍命中
    assert key(extraction_profile=replace(BIOMEDICAL, description="只改说明")) == key()
    # 其余输入各自参与键
    assert key(checksum="other") != key()
    assert key(llm_model="m2") != key()
    assert key(chunk_strategy="structural") != key()
    assert key(graph_extraction=False) != key()
    assert key(extraction_profile=None) != key()


def test_legal_prompt_judge_policy_and_relation_directions_are_consistent():
    system, _ = _system_prompt(LEGAL)
    # 法官口径统一：person 说明与 exclude_hint 都说审判人员不抽，不再出现「法官」属于 person 的说法
    person_label = dict(LEGAL.entity_types)["person"]
    assert "不含法官" in person_label and "法官、律师" not in person_label
    assert "审判长/审判员/法官/书记员" in LEGAL.exclude_hint and "一律不抽" in LEGAL.exclude_hint
    # 方向不明的关系都在 priority_hint 里说明了 head→tail
    for rel in ("REPRESENTS", "RULED_BY", "APPEALS", "CONTRACTS_WITH", "PLAINTIFF_IN"):
        assert rel in LEGAL.relation_types
        assert f"{rel} 为" in LEGAL.priority_hint or f"/{rel}" in LEGAL.priority_hint, rel
    assert "head→tail" in system
    # 案号自带括号属于名称本身，与「不带括号注释」不冲突
    assert "案号自带的括号" in LEGAL.name_rules and "原样保留" in LEGAL.name_rules
    assert "不带括号注释；法律法规用全称" in system


def test_general_profile_keeps_standards_about_references():
    # 讲「参考文献」的正规标准不是本文的参考文献条目（通用配置不启用论文元数据词表）
    desc = "信息与文献 参考文献著录规则的国家标准"
    assert noise_reason("GB/T 7714-2015", "work", desc, profile=GENERAL) is None
    assert noise_reason("GB/T 7714-2015", "work", desc, profile=LEGAL) is None
    # 明确是「本文参考文献列表里的条目」仍拦截
    assert (
        noise_reason("A survey", "work", "本文参考文献中的一篇综述", profile=GENERAL)
        == "reference_or_section"
    )
    assert noise_reason("X", "work", "Cited in the references", profile=GENERAL)
    # 论文配置下词表更宽，维持 v4 行为
    for profile in (ACADEMIC, BIOMEDICAL):
        assert noise_reason("A survey", "work", desc, profile=profile) == "reference_or_section"


def test_initials_and_anonymous_parties_only_filtered_in_academic_layer():
    # 缩写署名属论文体裁：学术配置丢弃，其它领域里 A、B 可以是法律文书的匿名当事人
    for name in ("A", "JFK", "N.C.", "Nielsen,M."):
        for profile in (ACADEMIC, BIOMEDICAL):
            assert (
                noise_reason(name, "person", None, profile=profile) == "person_citation_or_initials"
            ), (name, profile.key)
        for profile in (GENERAL, LEGAL):
            assert noise_reason(name, "person", "案件当事人", profile=profile) is None, name
    # "et al" 引文式署名是确定无误的拦截，所有配置都生效
    for profile in (GENERAL, LEGAL, ACADEMIC):
        assert noise_reason("Perez et al.", "person", None, profile=profile)


def test_general_reference_rule_requires_entry_semantics():
    # 国标 GB/T 7714 的描述里带「参考文献中」不是「本文参考文献条目」
    desc = "规定参考文献中各项著录格式的国家标准"
    for profile in (GENERAL, LEGAL):
        assert noise_reason("GB/T 7714-2015", "work", desc, profile=profile) is None
    assert noise_reason("A survey", "work", "文后参考文献的一篇综述", profile=GENERAL)
    assert noise_reason("A survey", "work", "参考文献列表中的条目", profile=GENERAL)
    assert noise_reason("A survey", "work", "参考文献条目", profile=GENERAL)


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeConfigSession:
    """load_extraction_profiles 只做一次 select(SystemConfig.value)。"""

    def __init__(self, value):
        self._value = value

    async def execute(self, _stmt):
        return _FakeScalarResult(self._value)


async def test_load_extraction_profiles_reads_custom_and_builtin():
    from loomvec.core.db.system_config import load_extraction_profiles

    profiles = await load_extraction_profiles(_FakeConfigSession({"patent": _custom_spec()}))  # type: ignore[arg-type]
    assert set(BUILTIN_PROFILES) < set(profiles) and "patent" in profiles
    # 库里没有该键 → 仅内置
    assert set(await load_extraction_profiles(_FakeConfigSession(None))) == set(BUILTIN_PROFILES)  # type: ignore[arg-type]


async def test_load_extraction_profiles_invalid_db_value_falls_back_to_builtin():
    from loomvec.core.db.system_config import load_extraction_profiles

    bad = {"patent": _custom_spec(entity_types=[{"type": "patent", "label": "专利"}])}  # 缺 other
    with capture_logs() as logs:
        profiles = await load_extraction_profiles(_FakeConfigSession(bad))  # type: ignore[arg-type]
    assert profiles == dict(BUILTIN_PROFILES)
    warn = [x for x in logs if x["event"] == "extraction_profiles_invalid"]
    assert len(warn) == 1 and warn[0]["log_level"] == "warning"
    # 引用了失效自定义配置的空间随之回落全局，不中断
    space = SimpleNamespace(extraction_profile="patent")
    assert resolve_extraction_profile(Settings(_env_file=None), space, profiles).key == "general"  # type: ignore[arg-type]
