import { useTranslation } from 'react-i18next';
import { StatusBadge } from './status-badge';

/** 审核状态徽标：pending_review / approved / rejected；null 显占位。 */
export function ReviewStatusTag({ status }: { status: string | null | undefined }) {
  const { t } = useTranslation();
  if (!status) return <StatusBadge>{t('reviewStatus.none')}</StatusBadge>;
  const tone = status === 'approved' ? 'green' : status === 'rejected' ? 'red' : ('amber' as const);
  return (
    <StatusBadge tone={tone}>{t(`reviewStatus.${status}`, { defaultValue: status })}</StatusBadge>
  );
}
