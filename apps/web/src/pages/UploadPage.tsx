import { useEffect, useRef, useState } from 'react';
import { CloudUpload, Inbox } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { api } from '@loomvec/sdk-ts';
import { useMySpaces } from '@/hooks';
import { extractApiError, formatBytes } from '@/utils';
import { uploadFile } from '@/upload';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { cn } from 'cn';

/**
 * P2-WEB-03 上传体验：配额进度 + 多文件队列（去重预检 → 预签名 → PUT 进度 → 登记）。
 * 每个文件一张状态卡：等待 / 去重检查 / 上传中（百分比）/ 已登记 / 完成 / 失败（含配额原因）。
 */

interface QueueItem {
  uid: string;
  name: string;
  size: number;
  status: 'waiting' | 'uploading' | 'done' | 'failed' | 'skipped';
  progress: number;
  error?: string;
}

/** antd Tag 色系（default/processing/success/error/warning）→ StatusBadge tone。 */
const STATUS_META: Record<QueueItem['status'], { tone: BadgeTone; text: string }> = {
  waiting: { tone: 'gray', text: '等待' },
  uploading: { tone: 'blue', text: '上传中' },
  done: { tone: 'green', text: '完成' },
  failed: { tone: 'red', text: '失败' },
  skipped: { tone: 'amber', text: '已跳过' },
};

const ACCEPT_TYPES = '.pdf,.docx,.pptx,.xlsx,.txt,.md,.markdown,.png,.jpg,.jpeg,.webp,.gif';

function percentOf(used: number, quota: number): number {
  if (quota <= 0) return 0;
  return Math.min(100, Math.round((used / quota) * 100));
}

export function UploadPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [items, setItems] = useState<QueueItem[]>([]);
  // 文件二进制按 uid 暂存（不进 React 状态）；同一 uid 只启动一次（防 StrictMode 双跑重复上传）
  const fileMapRef = useRef(new Map<string, File>());
  const startedRef = useRef(new Set<string>());
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  // 去重确认：resolve(true)=仍要上传，resolve(false)=放弃
  const [dupConfirm, setDupConfirm] = useState<{
    names: string[];
    resolve: (ok: boolean) => void;
  } | null>(null);

  const spaces = useMySpaces();
  const spaceName = (spaces.data ?? []).find((s) => s.id === spaceId)?.name;

  const usage = useQuery({
    queryKey: ['space-usage', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/usage', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载配额失败'));
      return data;
    },
  });

  const patchItem = (uid: string, patch: Partial<QueueItem>) =>
    setItems((prev) => prev.map((it) => (it.uid === uid ? { ...it, ...patch } : it)));

  const runItem = async (item: QueueItem) => {
    const file = fileMapRef.current.get(item.uid);
    if (!file) return;
    patchItem(item.uid, { status: 'uploading', progress: 0 });
    try {
      await uploadFile(file, {
        spaceId,
        onProgress: (pct) => patchItem(item.uid, { progress: pct }),
        onDuplicate: (existingNames) =>
          new Promise<boolean>((resolve) => {
            setDupConfirm({ names: existingNames, resolve });
          }),
      });
      patchItem(item.uid, { status: 'done', progress: 100 });
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
      void queryClient.invalidateQueries({ queryKey: ['space-usage', spaceId] });
    } catch (e) {
      const msg = e instanceof Error ? e.message : '上传失败';
      patchItem(item.uid, { status: msg === '已跳过上传' ? 'skipped' : 'failed', error: msg });
    }
  };

  // 队列调度：串行处理，任一时刻只有一个文件在上传
  useEffect(() => {
    const next = items.find((it) => it.status === 'waiting');
    if (!next || startedRef.current.has(next.uid)) return;
    startedRef.current.add(next.uid);
    void runItem(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const addFiles = (files: File[]) => {
    const fresh = files.map((f) => ({ uid: crypto.randomUUID(), file: f }));
    for (const { uid, file } of fresh) fileMapRef.current.set(uid, file);
    setItems((prev) => [
      ...prev,
      ...fresh.map(({ uid, file }) => ({
        uid,
        name: file.name,
        size: file.size,
        status: 'waiting' as const,
        progress: 0,
      })),
    ]);
  };

  const clearFinished = () => {
    setItems((prev) => prev.filter((it) => it.status === 'waiting' || it.status === 'uploading'));
    fileMapRef.current.clear();
    startedRef.current.clear();
  };

  const u = usage.data;
  const anyActive = items.some((it) => it.status === 'waiting' || it.status === 'uploading');

  const openFilePicker = () => inputRef.current?.click();

  return (
    <div className="space-y-4">
      <PageHeader
        title={`上传到 ${spaceName ?? '空间'}`}
        actions={
          <Button variant="outline" onClick={() => navigate(`/s/${spaceId}/assets`)}>
            返回资产
          </Button>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>空间配额</CardTitle>
        </CardHeader>
        <CardContent>
          {usage.isLoading ? (
            <p className="text-sm text-muted-foreground">加载配额中…</p>
          ) : usage.isError ? (
            <p className="text-sm text-destructive">{(usage.error as Error).message}</p>
          ) : u ? (
            <div className="flex flex-wrap gap-12">
              <div className="w-full max-w-[320px] space-y-1.5">
                <p className="text-sm">
                  存储用量：{formatBytes(u.storage_bytes)} /{' '}
                  {u.quota_storage_bytes > 0 ? formatBytes(u.quota_storage_bytes) : '不限额'}
                </p>
                <Progress
                  value={percentOf(u.storage_bytes, u.quota_storage_bytes)}
                  className={cn(
                    u.quota_storage_bytes > 0 &&
                      u.storage_bytes >= u.quota_storage_bytes &&
                      '[&_[data-slot=progress-indicator]]:bg-destructive',
                  )}
                />
              </div>
              <div className="w-full max-w-[320px] space-y-1.5">
                <p className="text-sm">
                  文件数：{u.file_count} / {u.quota_file_count > 0 ? u.quota_file_count : '不限额'}
                </p>
                <Progress
                  value={percentOf(u.file_count, u.quota_file_count)}
                  className={cn(
                    u.quota_file_count > 0 &&
                      u.file_count >= u.quota_file_count &&
                      '[&_[data-slot=progress-indicator]]:bg-destructive',
                  )}
                />
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-6">
          {/* 上传拖拽区（替代 Upload.Dragger）：支持拖拽与点击多选，整批按顺序入队 */}
          <div
            role="button"
            tabIndex={0}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              addFiles(Array.from(e.dataTransfer.files));
            }}
            onClick={openFilePicker}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                openFilePicker();
              }
            }}
            className={cn(
              'flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 hover:border-primary/50 hover:bg-muted/50',
              dragOver && 'border-primary bg-primary/5',
            )}
          >
            <Inbox className="size-8 text-muted-foreground" />
            <p className="text-sm font-medium">拖拽或点击选择文件（支持多选，逐个串行上传）</p>
            <p className="text-sm text-muted-foreground">
              上传前先做同空间去重预检；配额超限时将明确提示原因。
            </p>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept={ACCEPT_TYPES}
              className="hidden"
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => {
                addFiles(Array.from(e.target.files ?? []));
                e.target.value = '';
              }}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold">上传队列</p>
              {anyActive && <StatusBadge tone="blue">处理中</StatusBadge>}
              <Button variant="outline" size="sm" disabled={anyActive} onClick={clearFinished}>
                清理已完成
              </Button>
            </div>
            {items.length === 0 ? (
              <EmptyState title="尚未添加文件" />
            ) : (
              <div className="divide-y rounded-lg border">
                {items.map((it) => (
                  <div key={it.uid} className="flex items-start justify-between gap-3 px-4 py-3">
                    <div className="min-w-0 flex-1 space-y-1.5">
                      <div className="flex items-center gap-2">
                        <CloudUpload className="size-4 shrink-0 text-muted-foreground" />
                        <span className="max-w-[420px] truncate text-sm">{it.name}</span>
                        <span className="shrink-0 text-sm text-muted-foreground">
                          {formatBytes(it.size)}
                        </span>
                      </div>
                      {it.status === 'uploading' && (
                        <Progress value={it.progress} className="max-w-[420px]" />
                      )}
                      {it.error && <p className="text-sm text-destructive">{it.error}</p>}
                      {it.status === 'done' && (
                        <p className="text-sm text-muted-foreground">已登记并进入解析管线。</p>
                      )}
                    </div>
                    <StatusBadge tone={STATUS_META[it.status].tone} className="shrink-0">
                      {STATUS_META[it.status].text}
                    </StatusBadge>
                  </div>
                ))}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* 去重确认（替代 modal.confirm）：仍要上传 / 放弃 */}
      <Dialog
        open={dupConfirm !== null}
        onOpenChange={(open) => {
          if (!open) {
            dupConfirm?.resolve(false);
            setDupConfirm(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>发现疑似重复文件</DialogTitle>
            <DialogDescription>
              本空间已存在相同内容：{dupConfirm?.names.join('、')}。仍要上传这份副本吗？
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                dupConfirm?.resolve(false);
                setDupConfirm(null);
              }}
            >
              放弃
            </Button>
            <Button
              onClick={() => {
                dupConfirm?.resolve(true);
                setDupConfirm(null);
              }}
            >
              仍要上传
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
