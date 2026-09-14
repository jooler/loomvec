/**
 * 写操作二次确认 + 必填理由（审计留痕要求见 docs/04 §六）。
 * 仅用于后端接口确实接收 reason 的操作；不接收 reason 的危险操作用 ConfirmAction。
 */
import { useEffect, useState } from 'react';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Textarea } from '@loomvec/ui/components/ui/textarea';

export function ReasonModal(props: {
  open: boolean;
  title: string;
  /** 操作说明（展示在理由输入框上方） */
  description?: string;
  okText?: string;
  danger?: boolean;
  /** 理由是否必填（后端接口要求 reason 的操作必填；仅二次确认的操作可关） */
  requireReason?: boolean;
  confirmLoading?: boolean;
  onOk: (reason: string) => void | Promise<void>;
  onCancel: () => void;
  /** 额外表单（配额调整等），嵌入理由输入框下方 */
  children?: React.ReactNode;
}) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState(false);
  const requireReason = props.requireReason !== false;

  // 关闭时清空（对应旧 antd destroyOnHidden + resetFields）
  useEffect(() => {
    if (!props.open) {
      setReason('');
      setError(false);
    }
  }, [props.open]);

  const submit = async () => {
    if (requireReason && !reason.trim()) {
      setError(true);
      return;
    }
    await props.onOk(reason.trim());
  };

  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{props.title}</DialogTitle>
          {props.description && <DialogDescription>{props.description}</DialogDescription>}
        </DialogHeader>
        <div className="space-y-1.5">
          <label className="text-sm font-medium">操作理由</label>
          <Textarea
            rows={3}
            maxLength={500}
            value={reason}
            aria-invalid={error || undefined}
            placeholder="请填写操作理由（将记入审计日志）"
            onChange={(e) => {
              setReason(e.target.value);
              if (error) setError(false);
            }}
          />
          <div className="flex items-center justify-between">
            {error ? (
              <p className="text-sm text-destructive">请填写操作理由（将记入审计日志）</p>
            ) : (
              <span />
            )}
            <span className="text-xs text-muted-foreground">{reason.length}/500</span>
          </div>
        </div>
        {props.children}
        <DialogFooter>
          <Button variant="outline" onClick={props.onCancel} disabled={props.confirmLoading}>
            取消
          </Button>
          <Button
            variant={props.danger ? 'destructive' : 'default'}
            onClick={() => void submit()}
            disabled={props.confirmLoading}
          >
            {props.okText ?? '确认执行'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
