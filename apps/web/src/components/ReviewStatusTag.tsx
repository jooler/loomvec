import { REVIEW_STATUS_META } from '@/utils';
import { StatusBadge } from '@loomvec/ui/components/status-badge';

/** 审核状态徽标：pending_review / approved / rejected；null 显占位。 */
export function ReviewStatusTag({ status }: { status: string | null | undefined }) {
  if (!status) return <StatusBadge>无审核</StatusBadge>;
  const meta = REVIEW_STATUS_META[status];
  const tone =
    status === 'approved' ? 'green' : status === 'rejected' ? 'red' : ('amber' as const);
  return <StatusBadge tone={tone}>{meta?.text ?? status}</StatusBadge>;
}
