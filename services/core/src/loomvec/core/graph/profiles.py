"""抽取领域配置（ExtractionProfile）：合并抽取 prompt 的领域插槽 + 兜底过滤开关。

抽取底座（`pipeline/extraction.py` 的分片契约、锚点实体目标、数量预算、命名与关系通用规则）
与领域无关；「抽哪些类型、优先抽什么、不要抽什么、推荐哪些关系、确定性拦截哪些关系、
命名额外规则、示例」以及「是否屏蔽论文后置章节（pipeline/sections）」「是否启用论文元数据
噪音词表（graph/entity_filter）」都由领域配置给出。

- 内置四份（`BUILTIN_PROFILES`）：general（通用文档，全局默认）/ academic（学术论文）/
  biomedical（生物医药论文，在 academic 上叠加）/ legal（法律文书，初版）；
- 自定义配置存 system_config `pipeline.extraction_profiles`（{key: 配置 JSON}），
  经 `parse_custom_profiles` 严格校验，不得覆盖内置 key；
- 空间经 `Space.extraction_profile` 选择（NULL = 跟随全局 `pipeline.extraction_profile`），
  解析见 `pipeline.base.resolve_extraction_profile`；
- `fingerprint` 是影响抽取结果的内容字段的稳定哈希：入分片缓存键、落 version_meta，
  配置内容一改，同一 key 下的资产重跑也不会命中旧缓存。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from loomvec.core.constants import DEFAULT_ENTITY_TYPE, EXTRACTION_PROFILE_KEY_PATTERN
from loomvec.core.graph.ontology import ENTITY_TYPE_PATTERN, canonical_type

# system_config 中自定义配置的键（admin 设置项，见 api services/admin_settings.py）
CUSTOM_PROFILES_SETTING_KEY = "pipeline.extraction_profiles"
MAX_CUSTOM_PROFILES = 20

# 关系类型的合法写法（配置 key 见 constants.EXTRACTION_PROFILE_KEY_PATTERN，
# 实体类型见 ontology.ENTITY_TYPE_PATTERN）
RELATION_TYPE_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"


@dataclass(frozen=True)
class ExtractionProfile:
    """一份领域配置（内置与自定义同构；字段含义见模块文档）。"""

    key: str
    label: str  # 中文名（下拉展示；也写进 prompt 说明文档领域）
    description: str
    entity_types: tuple[tuple[str, str], ...]  # ((type 小写 slug, 中文说明), ...)，含 other
    priority_hint: str  # 优先抽什么
    exclude_hint: str  # 不要抽什么（领域部分；通用排除项在底座里）
    relation_types: tuple[str, ...]  # 推荐关系集合
    blocked_relations: tuple[str, ...]  # 确定性拦截（解析时丢弃）
    name_rules: str  # 命名额外规则（如基因符号大小写）
    # 示例须自洽：{"entities": [{name,type,aliases,description}, ...≥2 个],
    #            "relation": {head,tail,type}}，head/tail 必须是 entities 里的 name
    example: dict[str, Any]
    skip_back_matter: bool = False  # 屏蔽参考文献/致谢/基金等论文后置章节（pipeline/sections）
    academic_noise: bool = False  # 启用论文元数据噪音词表（graph/entity_filter）
    builtin: bool = True

    @property
    def type_keys(self) -> tuple[str, ...]:
        return tuple(t for t, _ in self.entity_types)

    @property
    def blocked_set(self) -> frozenset[str]:
        return frozenset(self.blocked_relations)

    def content(self) -> dict[str, Any]:
        """影响抽取结果的内容字段（指纹输入；key/description/builtin 不影响抽取）。"""
        return {
            "label": self.label,
            "entity_types": [list(t) for t in self.entity_types],
            "priority_hint": self.priority_hint,
            "exclude_hint": self.exclude_hint,
            "relation_types": list(self.relation_types),
            "blocked_relations": list(self.blocked_relations),
            "name_rules": self.name_rules,
            "example": self.example,
            "skip_back_matter": self.skip_back_matter,
            "academic_noise": self.academic_noise,
        }

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.content(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 内置配置
# ---------------------------------------------------------------------------

GENERAL = ExtractionProfile(
    key="general",
    label="通用文档",
    description="一般文档（手册、报告、新闻、网页、笔记等）：抽取具名的人物、机构、地点、事件、"
    "产品、作品与概念，不屏蔽任何章节",
    entity_types=(
        ("person", "人物"),
        ("organization", "组织/机构（公司、政府机关、学校、团体等）"),
        ("location", "地点（国家、城市、地址、地理实体）"),
        ("event", "具名事件（会议、项目、历史事件等）"),
        ("product", "产品（具名的产品、设备、软件、服务）"),
        ("work", "具名作品/文件/标准（书籍、文章、报告、标准、规范等）"),
        ("concept", "领域公认的概念、术语、理论"),
        ("method", "具名的方法/算法/技术/流程"),
        ("dataset", "具名的数据集/数据库"),
        ("other", "其他"),
    ),
    priority_hint="文中承担实质角色的具名人物 person 与组织机构 organization、地点 location、"
    "事件 event、产品 product、作品/文件/标准 work，以及领域公认的概念 concept、"
    "具名的方法/技术 method、具名的数据集/数据库 dataset。",
    exclude_hint="联系方式（电话、邮箱、网址）；日期、金额、数量等数值本身",
    relation_types=(
        "PART_OF",
        "IS_A",
        "MEMBER_OF",
        "LOCATED_IN",
        "USES",
        "CAUSES",
        "CREATED_BY",
        "OWNS",
        "PARTICIPATED_IN",
        "DERIVED_FROM",
        "COMPARED_WITH",
        "ASSOCIATED_WITH",
    ),
    # 通用文档里作者/引用/出版也可能是现实世界知识（某书的作者、某标准引用另一标准），
    # 无法确定性区分「本文档自身的署名/出版信息」，故不做关系拦截，只在 prompt 中排除
    blocked_relations=(),
    name_rules="",
    example={
        "entities": [
            {
                "name": "International Organization for Standardization",
                "type": "organization",
                "aliases": ["ISO"],
                "description": "制定国际标准的非政府组织",
            },
            {
                "name": "ISO 9001",
                "type": "work",
                "aliases": [],
                "description": "质量管理体系要求的国际标准",
            },
        ],
        "relation": {
            "head": "ISO 9001",
            "tail": "International Organization for Standardization",
            "type": "CREATED_BY",
        },
    },
)

# 论文场景的类型：general 去掉 work（论文里的「作品」基本是被引文献，属噪音）
_ACADEMIC_TYPES: tuple[tuple[str, str], ...] = tuple(
    t for t in GENERAL.entity_types if t[0] != "work"
)

ACADEMIC = ExtractionProfile(
    key="academic",
    label="学术论文",
    description="任意学科的学术论文：屏蔽参考文献/致谢/基金等后置章节，不抽作者、单位、基金、"
    "期刊等论文元数据，优先抽方法与数据集",
    entity_types=_ACADEMIC_TYPES,
    priority_hint="具名的方法/算法/模型/软件/实验技术 method、具名的数据集/数据库/队列 dataset，"
    "以及领域公认的概念 concept；person/organization/location/event 一般不抽"
    "（作者、单位、基金、厂商一律不抽），除非整篇研究就围绕它。",
    exclude_hint="样本/批次编号；数据库登录号；通用统计检验",
    relation_types=(
        "USES",
        "PART_OF",
        "IS_A",
        "CAUSES",
        "ASSOCIATED_WITH",
        "DERIVED_FROM",
        "TRAINED_ON",
        "EVALUATED_ON",
        "COMPARED_WITH",
        "OUTPERFORMS",
        "LOCATED_IN",
    ),
    # 论文元数据型关系：署名、单位、资助、出版、引用
    blocked_relations=(
        "AFFILIATED_WITH",
        "FUNDED_BY",
        "PUBLISHED_IN",
        "SPONSORED_BY",
        "AUTHORED_BY",
        "AUTHOR_OF",
        "CITED_BY",
        "CITES",
        "EMPLOYED_BY",
        "WORKS_AT",
    ),
    name_rules="",
    example={
        "entities": [
            {
                "name": "Convolutional neural network",
                "type": "method",
                "aliases": ["CNN"],
                "description": "以卷积层提取局部特征的神经网络",
            },
            {
                "name": "ResNet",
                "type": "method",
                "aliases": [],
                "description": "引入残差连接的深层卷积网络",
            },
        ],
        "relation": {"head": "ResNet", "tail": "Convolutional neural network", "type": "IS_A"},
    },
    skip_back_matter=True,
    academic_noise=True,
)

BIOMEDICAL = replace(
    ACADEMIC,
    key="biomedical",
    label="生物医药论文",
    description="生物医学/生命科学论文：在学术论文配置上增加基因/蛋白、疾病、细胞类型、"
    "药物/化合物类型，排除抗体、试剂、质粒与实验操作步骤",
    entity_types=(
        ("person", "人物"),
        ("organization", "组织/机构"),
        ("product", "产品"),
        ("location", "地点"),
        ("event", "事件"),
        ("concept", "领域公认的概念"),
        ("gene_protein", "基因/蛋白"),
        ("disease", "疾病/表型"),
        ("cell_type", "细胞类型/组织"),
        ("compound", "药物/化合物"),
        ("method", "具名的方法/算法/模型/软件/实验技术"),
        ("dataset", "具名的数据集/数据库/队列"),
        ("other", "其他"),
    ),
    priority_hint="基因/蛋白 gene_protein、疾病/表型 disease、细胞类型/组织 cell_type、"
    "药物/化合物 compound、具名的方法/算法/模型/软件/实验技术 method、"
    "具名的数据集/数据库/队列 dataset，以及领域公认的概念 concept；"
    "person/organization/location/event 一般不抽（作者、单位、基金、厂商一律不抽），"
    "除非整篇研究就围绕它。",
    exclude_hint="cells、patients、mice 这类宽泛词；样本/患者/批次编号；数据库登录号；"
    "通用统计检验；抗体、试剂、耗材、质粒及其货号；细胞培养/转染/文库构建等实验操作步骤",
    relation_types=(
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
    ),
    # 在论文元数据型关系之外，再拦试剂/耗材的供货与生产关系
    blocked_relations=(
        *ACADEMIC.blocked_relations,
        "SUPPLIED_BY",
        "MANUFACTURED_BY",
        "PROVIDED_BY",
        "PRODUCED_BY",
    ),
    name_rules="基因符号保持原文大小写",
    example={
        "entities": [
            {
                "name": "Epithelial-to-mesenchymal transition",
                "type": "concept",
                "aliases": ["EMT"],
                "description": "上皮细胞失去极性获得迁移能力的过程",
            },
            {
                "name": "TGFB1",
                "type": "gene_protein",
                "aliases": [],
                "description": "调控细胞生长与分化的细胞因子",
            },
        ],
        "relation": {
            "head": "TGFB1",
            "tail": "Epithelial-to-mesenchymal transition",
            "type": "REGULATES",
        },
    },
)

LEGAL = ExtractionProfile(
    key="legal",
    label="法律文书",
    description="法律法规、裁判文书、合同等法律文本（初版，建议领域专家审阅）：抽取当事人与机构、"
    "法律条款、案件、案由、合同及其引用/适用/当事关系",
    entity_types=(
        ("person", "自然人（当事人、律师、证人；不含法官）"),
        ("organization", "组织/机构（公司、法院、检察院、行政机关等）"),
        ("statute", "法律法规及具体条款"),
        ("case", "案件/判例（案号或案件名称）"),
        ("cause", "案由/罪名"),
        ("contract", "合同/协议"),
        ("location", "地点"),
        ("event", "对案件有实质意义的事件/行为（签约、违约、事故等）"),
        ("concept", "法律概念/原则"),
        ("other", "其他"),
    ),
    priority_hint="具名的当事人与机构 person/organization（原告、被告、第三人、法院、行政机关、"
    "公司等所指的具体主体）、被引用或适用的法律法规及条款 statute、案件/判例 case、"
    "案由/罪名 cause、合同/协议 contract，以及对案件有实质意义的事件 event 与法律概念 concept。"
    "关系方向（head→tail）：PARTY_TO/PLAINTIFF_IN/DEFENDANT_IN 为当事人→案件；"
    "REPRESENTS 为代理人（律师、律所）→被代理人；RULED_BY 为案件→审理法院；"
    "APPEALS 为上诉案→被上诉的原审案；CITES/APPLIES/VIOLATES 为案件或主体→所引用、适用、"
    "违反的法规条款；AMENDS/REPEALS/SUPERSEDES 为新法规→被修订、废止、取代的旧法规；"
    "INTERPRETS 为解释性文件→被解释的法规；LIABLE_FOR 为责任主体→所负责任的事由；"
    "CONTRACTS_WITH 为合同双方（不分先后）。",
    exclude_hint="审判长/审判员/法官/书记员等审判人员（含文末署名，一律不抽）；"
    "文书格式性内容（送达与落款套话、页眉页脚）；"
    "「原告」「被告」「上诉人」「本院」这类泛称本身（应抽其所指的具体主体）；"
    "金额、日期等数值本身",
    relation_types=(
        "CITES",
        "APPLIES",
        "INTERPRETS",
        "AMENDS",
        "REPEALS",
        "SUPERSEDES",
        "VIOLATES",
        "PARTY_TO",
        "PLAINTIFF_IN",
        "DEFENDANT_IN",
        "REPRESENTS",
        "RULED_BY",
        "APPEALS",
        "CONTRACTS_WITH",
        "EMPLOYED_BY",
        "SUPPLIED_BY",
        "OWNS",
        "LIABLE_FOR",
        "PART_OF",
        "LOCATED_IN",
    ),
    blocked_relations=(),
    name_rules="法律法规用全称、简称放入 aliases；具体条款写成「法规名+第X条」；"
    "案件优先用案号，案号自带的括号（如「（2023）京01民终1234号」）属于名称本身，须原样保留",
    example={
        "entities": [
            {
                "name": "中华人民共和国民法典",
                "type": "statute",
                "aliases": ["民法典"],
                "description": "调整平等主体间民事关系的基本法律",
            },
            {
                "name": "（2023）京01民终1234号",
                "type": "case",
                "aliases": [],
                "description": "一起民事二审案件",
            },
        ],
        "relation": {
            "head": "（2023）京01民终1234号",
            "tail": "中华人民共和国民法典",
            "type": "APPLIES",
        },
    },
)

BUILTIN_PROFILES: dict[str, ExtractionProfile] = {
    p.key: p for p in (GENERAL, ACADEMIC, BIOMEDICAL, LEGAL)
}


# ---------------------------------------------------------------------------
# 自定义配置：严格校验（system_config 写入前与 worker 读取时共用）
# ---------------------------------------------------------------------------


class _EntityTypeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    label: str = Field(min_length=1, max_length=32)


class _ExampleEntitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    type: str
    aliases: list[str] = Field(default_factory=list, max_length=5)
    description: str = Field(min_length=1, max_length=80)


class _ExampleRelationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    head: str = Field(min_length=1, max_length=80)
    tail: str = Field(min_length=1, max_length=80)
    type: str = Field(pattern=RELATION_TYPE_PATTERN)


class _ExampleSpec(BaseModel):
    """示例须自洽：关系的 head/tail 必须是示例 entities 里的 name（与 prompt 对输出的要求一致，
    否则模型照抄示例会产出头尾不在实体表里的关系，被解析静默丢弃）。"""

    model_config = ConfigDict(extra="forbid")

    entities: list[_ExampleEntitySpec] = Field(min_length=2, max_length=4)
    relation: _ExampleRelationSpec

    @model_validator(mode="after")
    def _relation_endpoints_in_entities(self) -> _ExampleSpec:
        names = [e.name for e in self.entities]
        if len(set(names)) != len(names):
            raise ValueError("example.entities 中的 name 不能重复")
        for side in ("head", "tail"):
            value = getattr(self.relation, side)
            if value not in names:
                raise ValueError(
                    f"example.relation.{side} 须是 example.entities 里的 name：{value!r}"
                )
        if self.relation.head == self.relation.tail:
            raise ValueError("example.relation 的 head 与 tail 不能是同一个实体")
        return self


class _ProfileSpec(BaseModel):
    """自定义配置 JSON 的结构（与 ExtractionProfile 字段一一对应，key 取外层字典键）。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=200)
    entity_types: list[_EntityTypeSpec] = Field(min_length=1, max_length=24)
    priority_hint: str = Field(min_length=1, max_length=600)
    exclude_hint: str = Field(default="", max_length=600)
    relation_types: list[str] = Field(min_length=1, max_length=40)
    blocked_relations: list[str] = Field(default_factory=list, max_length=40)
    name_rules: str = Field(default="", max_length=200)
    example: _ExampleSpec
    skip_back_matter: bool = False
    academic_noise: bool = False

    @field_validator("relation_types", "blocked_relations")
    @classmethod
    def _relation_list(cls, value: list[str]) -> list[str]:
        for rel in value:
            if not re.match(RELATION_TYPE_PATTERN, rel):
                raise ValueError(f"关系类型须为大写字母开头的大写下划线短语：{rel!r}")
        if len(set(value)) != len(value):
            raise ValueError("关系类型不能重复")
        return value

    @model_validator(mode="after")
    def _cross_check(self) -> _ProfileSpec:
        types = [t.type for t in self.entity_types]
        if len(set(types)) != len(types):
            raise ValueError("entity_types 中的类型不能重复")
        if DEFAULT_ENTITY_TYPE not in types:
            raise ValueError(f"entity_types 须包含兜底类型 {DEFAULT_ENTITY_TYPE}")
        for ent in self.example.entities:
            if ent.type not in types:
                raise ValueError(
                    f"example.entities 的 type 须是 entity_types 中的类型：{ent.type!r}"
                )
        overlap = set(self.relation_types) & set(self.blocked_relations)
        if overlap:
            raise ValueError(f"关系不能同时在推荐集合与拦截集合中：{sorted(overlap)}")
        # 示例关系须在推荐集合内；推荐与拦截集合互斥（上面已校验），故也必然不在拦截集合内
        rel_type = self.example.relation.type
        if rel_type not in self.relation_types:
            raise ValueError(f"example.relation.type 须是 relation_types 中的关系：{rel_type!r}")
        return self


def _format_pydantic_error(key: str, err: PydanticValidationError) -> str:
    first = err.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "")).removeprefix("Value error, ")
    return f"{key}{'.' + loc if loc else ''}: {msg}"


def parse_custom_profiles(value: Any) -> dict[str, ExtractionProfile]:
    """校验并解析 system_config 中的自定义配置（{key: 配置 JSON}）。

    不合法抛 ValueError（中文原因）；None / {} 表示没有自定义配置。
    """
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("自定义抽取配置须为 JSON 对象：{配置 key: 配置内容}")
    if len(value) > MAX_CUSTOM_PROFILES:
        raise ValueError(f"自定义抽取配置最多 {MAX_CUSTOM_PROFILES} 份")
    out: dict[str, ExtractionProfile] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not re.match(EXTRACTION_PROFILE_KEY_PATTERN, key):
            raise ValueError(f"配置 key 须为小写字母开头的小写字母/数字/下划线（2~32 位）：{key!r}")
        if key in BUILTIN_PROFILES:
            raise ValueError(f"配置 key 不能覆盖内置配置：{key}")
        try:
            spec = _ProfileSpec.model_validate(raw)
        except PydanticValidationError as e:
            raise ValueError(_format_pydantic_error(key, e)) from e
        out[key] = ExtractionProfile(
            key=key,
            label=spec.label,
            description=spec.description,
            entity_types=tuple((t.type, t.label) for t in spec.entity_types),
            priority_hint=spec.priority_hint,
            exclude_hint=spec.exclude_hint,
            relation_types=tuple(spec.relation_types),
            blocked_relations=tuple(spec.blocked_relations),
            name_rules=spec.name_rules,
            example=spec.example.model_dump(),
            skip_back_matter=spec.skip_back_matter,
            academic_noise=spec.academic_noise,
            builtin=False,
        )
    return out


def available_profiles(custom_value: Any) -> dict[str, ExtractionProfile]:
    """内置 + 自定义（custom_value 为 system_config 原值；不合法抛 ValueError）。"""
    return {**BUILTIN_PROFILES, **parse_custom_profiles(custom_value)}


def profile_entity_type(profile: ExtractionProfile, raw_type: Any) -> str:
    """LLM 输出的类型 → 该配置白名单内的类型；不在白名单内落 other。"""
    t = canonical_type(raw_type)
    return t if t in profile.type_keys else DEFAULT_ENTITY_TYPE
