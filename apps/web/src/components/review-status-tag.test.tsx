import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { t } from '@/i18n';
import { ReviewStatusTag } from './review-status-tag';

describe('ReviewStatusTag', () => {
  it('renders known review status', () => {
    render(<ReviewStatusTag status="pending_review" />);
    expect(screen.getByText(t('ui:reviewStatus.pending_review'))).toBeTruthy();
  });

  it('renders placeholder for empty status', () => {
    render(<ReviewStatusTag status={null} />);
    expect(screen.getByText(t('ui:reviewStatus.none'))).toBeTruthy();
  });

  it('renders unknown status as-is', () => {
    render(<ReviewStatusTag status="future_state" />);
    expect(screen.getByText('future_state')).toBeTruthy();
  });
});
