import { StatusBadge } from './status-badge';

/** 审核状态字典（P2 空间先审后见；web 与运营端共用）。 */
const REVIEW_STATUS_META: Record<string, { text: string }> = {
  pending_review: { text: '待审核' },
  approved: { text: '已通过' },
  rejected: { text: '已驳回' },
};

/** 审核状态徽标：pending_review / approved / rejected；null 显占位。 */
export function ReviewStatusTag({ status }: { status: string | null | undefined }) {
  if (!status) return <StatusBadge>无审核</StatusBadge>;
  const meta = REVIEW_STATUS_META[status];
  const tone = status === 'approved' ? 'green' : status === 'rejected' ? 'red' : ('amber' as const);
  return <StatusBadge tone={tone}>{meta?.text ?? status}</StatusBadge>;
}
