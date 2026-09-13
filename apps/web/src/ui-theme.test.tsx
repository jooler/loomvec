import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@loomvec/ui/components/ui/dropdown-menu';
import { ThemeProvider } from '@loomvec/ui/components/theme-provider';
import { ThemeToggle } from '@loomvec/ui/components/mode-toggle';

afterEach(() => {
  localStorage.clear();
  document.documentElement.className = '';
});

describe('Button 作为 Radix asChild 触发器', () => {
  // 回归：React 18 下 Button 未用 forwardRef 时，Slot 传 ref 会在挂载期报
  // "Function components cannot be given refs"（见运维端/用户端布局启动告警）
  it('不再触发 function-component ref 警告', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button>触发器</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem>菜单项</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    expect(screen.getByRole('button', { name: '触发器' })).toBeInTheDocument();
    expect(errorSpy).not.toHaveBeenCalledWith(expect.stringContaining('cannot be given refs'));
    errorSpy.mockRestore();
  });
});

describe('ThemeProvider + ThemeToggle', () => {
  it('点击在明暗间切换（class 挂在 <html> 上）', async () => {
    render(
      <ThemeProvider defaultTheme="light" enableSystem={false}>
        <ThemeToggle />
      </ThemeProvider>,
    );
    const toggle = screen.getByRole('button', { name: '切换明暗主题' });
    await waitFor(() =>
      expect(document.documentElement.classList.contains('dark')).toBe(false),
    );
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(document.documentElement.classList.contains('dark')).toBe(true),
    );
    expect(localStorage.getItem('theme')).toBe('dark');
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(document.documentElement.classList.contains('dark')).toBe(false),
    );
    expect(localStorage.getItem('theme')).toBe('light');
  });
});
