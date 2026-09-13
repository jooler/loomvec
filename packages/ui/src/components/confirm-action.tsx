import { useState } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from './ui/alert-dialog';

/**
 * 危险/重要操作确认（替代 antd Popconfirm）：trigger 直接传触发按钮元素。
 * 用法：
 *   <ConfirmAction
 *     trigger={<Button variant="outline" size="sm" disabled={!canWrite}>启用</Button>}
 *     title="确认启用该租户？" onConfirm={() => activate(r)} />
 */
export function ConfirmAction(props: {
  trigger: React.ReactElement;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  /** 确认按钮使用 destructive 样式 */
  danger?: boolean;
  /** 提交中禁用按钮（可选） */
  loading?: boolean;
  onConfirm: () => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const confirm = async () => {
    setBusy(true);
    try {
      await props.onConfirm();
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>{props.trigger}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{props.title}</AlertDialogTitle>
          {props.description && <AlertDialogDescription>{props.description}</AlertDialogDescription>}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{props.cancelText ?? '取消'}</AlertDialogCancel>
          <AlertDialogAction
            className={props.danger ? 'bg-destructive text-white hover:bg-destructive/90' : ''}
            disabled={busy || props.loading}
            onClick={(e) => {
              e.preventDefault();
              void confirm();
            }}
          >
            {props.confirmText ?? '确认'}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
