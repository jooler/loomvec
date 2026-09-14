import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReviewStatusTag } from './review-status-tag';

describe('ReviewStatusTag', () => {
  it('渲染已知审核状态', () => {
    render(<ReviewStatusTag status="pending_review" />);
    expect(screen.getByText('待审核')).toBeTruthy();
  });

  it('空状态渲染占位', () => {
    render(<ReviewStatusTag status={null} />);
    expect(screen.getByText('无审核')).toBeTruthy();
  });

  it('未知状态原样显示', () => {
    render(<ReviewStatusTag status="future_state" />);
    expect(screen.getByText('future_state')).toBeTruthy();
  });
});
