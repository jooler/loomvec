/** 一次性明文凭证展示（仅此一次可见），用于 API Key / client secret / signing secret。 */
import { Copy } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';

/** 一次性明文凭证字段。 */
export interface SecretField {
  label: string;
  value: string;
}

function copyText(v: string, copiedMsg: string, failedMsg: string) {
  void navigator.clipboard
    .writeText(v)
    .then(() => toast.success(copiedMsg))
    .catch(() => toast.error(failedMsg));
}

export function SecretModal(props: {
  open: boolean;
  title: string;
  fields: SecretField[];
  onClose: () => void;
}) {
  const { t } = useTranslation('components');
  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{props.title}</DialogTitle>
          <DialogDescription className="text-amber-600 dark:text-amber-400">
            {t('secret.warning')}
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
                  onClick={() => copyText(f.value, t('feedback.copied'), t('secret.copyFailed'))}
                >
                  <Copy />
                </Button>
              </div>
            </div>
          ))}
        </div>
        <DialogFooter>
          <Button onClick={props.onClose}>{t('secret.saved')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
