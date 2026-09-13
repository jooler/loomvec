"""P2-QA-01/02 纯逻辑单测：权限矩阵、配额决策、元数据校验、mock 图文向量。

DB 级矩阵与审核流链路见 test_integration_pg.py（testcontainers）。
"""

from __future__ import annotations

import pytest

from loomvec.core.authz import ROLE_RANK, at_least, decide_asset_visibility
from loomvec.core.db.models import ReviewStatus, SpaceRole
from loomvec.core.errors import ValidationError
from loomvec.core.quota import (
    QUOTA_SPACE_FILES,
    QUOTA_SPACE_STORAGE,
    QUOTA_TENANT_FILES,
    QUOTA_TENANT_STORAGE,
    QuotaSnapshot,
    decide_upload,
)
from loomvec.core.taxonomy import normalize_tag_name, validate_metadata

# ---------------------------------------------------------------------------
# P2-QA-01 权限矩阵（角色序数 × 审核可见性）
# ---------------------------------------------------------------------------


class TestRoleRank:
    def test_ordering(self):
        assert ROLE_RANK[SpaceRole.OWNER] > ROLE_RANK[SpaceRole.EDITOR]
        assert ROLE_RANK[SpaceRole.EDITOR] > ROLE_RANK[SpaceRole.VIEWER]

    def test_at_least_matrix(self):
        matrix = {
            ("viewer", "viewer"): True,
            ("viewer", "editor"): False,
            ("viewer", "owner"): False,
            ("editor", "viewer"): True,
            ("editor", "editor"): True,
            ("editor", "owner"): False,
            ("owner", "viewer"): True,
            ("owner", "editor"): True,
            ("owner", "owner"): True,
        }
        for (role, min_role), expected in matrix.items():
            assert at_least(role, min_role) is expected, (role, min_role)


class TestReviewVisibility:
    def test_viewer_sees_approved_in_review_space(self):
        assert decide_asset_visibility(
            review_required=True, review_status=ReviewStatus.APPROVED, role="viewer"
        )

    @pytest.mark.parametrize("status", [ReviewStatus.PENDING_REVIEW, ReviewStatus.REJECTED, None])
    def test_viewer_blocked_in_review_space(self, status):
        assert not decide_asset_visibility(
            review_required=True, review_status=status, role="viewer"
        )

    def test_viewer_unrestricted_without_review(self):
        for status in (ReviewStatus.PENDING_REVIEW, ReviewStatus.REJECTED, None):
            assert decide_asset_visibility(
                review_required=False, review_status=status, role="viewer"
            )

    @pytest.mark.parametrize("role", ["editor", "owner"])
    def test_editors_see_all_statuses(self, role):
        for status in (ReviewStatus.PENDING_REVIEW, ReviewStatus.REJECTED, None):
            assert decide_asset_visibility(review_required=True, review_status=status, role=role)


# ---------------------------------------------------------------------------
# P2-QA-02 配额决策矩阵
# ---------------------------------------------------------------------------


def _snap(
    *,
    t_storage=0,
    t_files=0,
    t_used_b=0,
    t_used_f=0,
    s_storage=0,
    s_files=0,
    s_used_b=0,
    s_used_f=0,
) -> QuotaSnapshot:
    return QuotaSnapshot(
        tenant_storage_bytes=t_storage,
        tenant_file_count=t_files,
        tenant_used_bytes=t_used_b,
        tenant_used_files=t_used_f,
        space_storage_bytes=s_storage,
        space_file_count=s_files,
        space_used_bytes=s_used_b,
        space_used_files=s_used_f,
    )


class TestQuotaDecisions:
    def test_unlimited_when_zero(self):
        assert decide_upload(_snap(), 10**12) is None

    def test_tenant_storage_exceeded(self):
        snap = _snap(t_storage=100, t_used_b=90)
        assert decide_upload(snap, 20) == QUOTA_TENANT_STORAGE

    def test_tenant_file_count_exceeded(self):
        assert decide_upload(_snap(t_files=3, t_used_f=3), 1) == QUOTA_TENANT_FILES

    def test_space_storage_exceeded(self):
        assert decide_upload(_snap(s_storage=50, s_used_b=40), 20) == QUOTA_SPACE_STORAGE

    def test_space_file_count_exceeded(self):
        assert decide_upload(_snap(s_files=2, s_used_f=2), 1) == QUOTA_SPACE_FILES

    def test_tenant_quota_not_affected_by_space_only_usage(self):
        # 租户不限 + 空间限 100：90+20 超
        assert decide_upload(_snap(s_storage=100, s_used_b=90), 20) == QUOTA_SPACE_STORAGE

    def test_exact_boundary_allowed(self):
        snap = _snap(t_storage=100, t_used_b=90, s_storage=100, s_used_b=90)
        assert decide_upload(snap, 10) is None


# ---------------------------------------------------------------------------
# P2-CORE-04 元数据 schema 校验
# ---------------------------------------------------------------------------


def _field(key, ftype, required=False, options=None):
    from loomvec.core.db.models import MetadataField, MetadataFieldType

    return MetadataField(
        space_id=None,
        key=key,
        name=key,
        field_type=MetadataFieldType(ftype),
        required=required,
        options=options or [],
    )


class TestMetadataValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            validate_metadata([_field("a", "text")], {"b": "x"})

    def test_required_missing_rejected(self):
        with pytest.raises(ValidationError):
            validate_metadata([_field("a", "text", required=True)], {})

    def test_number_coercion(self):
        out = validate_metadata([_field("n", "number")], {"n": "3.0"})
        assert out["n"] == 3
        out = validate_metadata([_field("n", "number")], {"n": 2.5})
        assert out["n"] == 2.5

    def test_number_invalid(self):
        with pytest.raises(ValidationError):
            validate_metadata([_field("n", "number")], {"n": "abc"})

    def test_date_iso(self):
        assert validate_metadata([_field("d", "date")], {"d": "2026-09-12"}) == {"d": "2026-09-12"}
        with pytest.raises(ValidationError):
            validate_metadata([_field("d", "date")], {"d": "昨天"})

    def test_select_options(self):
        f = _field("s", "select", options=["red", "blue"])
        assert validate_metadata([f], {"s": "red"}) == {"s": "red"}
        with pytest.raises(ValidationError):
            validate_metadata([f], {"s": "green"})

    def test_optional_absent_ok(self):
        assert validate_metadata([_field("a", "text")], {}) == {}

    def test_tag_normalize(self):
        assert normalize_tag_name("  AI   运维  ") == "AI 运维"


# ---------------------------------------------------------------------------
# P2-QA-01 资产管理边界（editor 仅限本人上传）
# ---------------------------------------------------------------------------


class TestAssetManageBoundary:
    def test_owner_full_access(self):
        from loomvec.core.authz import can_manage_asset

        assert can_manage_asset(role="owner", asset_created_by=None, user_id=None)
        assert can_manage_asset(role="owner", asset_created_by=OTHER, user_id=ALICE)

    def test_editor_only_own_assets(self):
        from loomvec.core.authz import can_manage_asset

        assert can_manage_asset(role="editor", asset_created_by=ALICE, user_id=ALICE)
        assert not can_manage_asset(role="editor", asset_created_by=OTHER, user_id=ALICE)
        assert not can_manage_asset(role="editor", asset_created_by=None, user_id=ALICE)

    def test_api_key_manages_only_unowned(self):
        from loomvec.core.authz import can_manage_asset

        assert can_manage_asset(role="editor", asset_created_by=None, user_id=None)
        assert not can_manage_asset(role="editor", asset_created_by=ALICE, user_id=None)

    def test_viewer_never(self):
        from loomvec.core.authz import can_manage_asset

        assert not can_manage_asset(role="viewer", asset_created_by=ALICE, user_id=ALICE)


ALICE = __import__("uuid").UUID("0198bec0-0000-7000-8000-000000000101")
OTHER = __import__("uuid").UUID("0198bec0-0000-7000-8000-000000000999")
