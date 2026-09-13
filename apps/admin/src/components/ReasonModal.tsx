/**
 * 写操作通用组件（shadcn 版）：ReasonModal（二次确认 + 必填理由，审计留痕要求见 docs/04 §六）
 * 与 SecretModal（一次性明文凭证展示，仅此一次可见）。
 */
import { useEffect, useState } from 'react';
import { Copy } from 'lucide-react';
import { toast } from 'sonner';
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

/** 一次性明文凭证字段。 */
export interface SecretField {
  label: string;
  value: string;
}

function copyText(v: string) {
  void navigator.clipboard
    .writeText(v)
    .then(() => toast.success('已复制'))
    .catch(() => toast.error('复制失败，请手动选择复制'));
}

export function SecretModal(props: {
  open: boolean;
  title: string;
  fields: SecretField[];
  onClose: () => void;
}) {
  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{props.title}</DialogTitle>
          <DialogDescription className="text-amber-600 dark:text-amber-400">
            以下凭证仅此一次展示，请立即复制保存；关闭后无法再次查看。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {props.fields.map((f) => (
            <div key={f.label} className="space-y-1">
              <p className="text-sm font-medium">{f.label}</p>
              <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2">
                <code className="min-w-0 flex-1 break-all font-mono text-sm">{f.value}</code>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7 shrink-0"
                  onClick={() => copyText(f.value)}
                >
                  <Copy />
                </Button>
              </div>
            </div>
          ))}
        </div>
        <DialogFooter>
          <Button onClick={props.onClose}>我已保存，关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
