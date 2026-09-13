import { useEffect, useMemo, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import type { ColumnDef } from '@tanstack/react-table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { Pencil, RefreshCw, Trash2, X } from 'lucide-react';
import { cn } from 'cn';
import { api } from '@loomvec/sdk-ts';
import { useSpaceCategories } from '@/hooks';
import { extractApiError } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { Separator } from '@loomvec/ui/components/ui/separator';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { MediaPlayer } from '@/components/media-player';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { ReviewStatusTag } from '@/components/ReviewStatusTag';
import { StatusBadge } from '@loomvec/ui/components/status-badge';

/**
 * P1 沿用：处理任务时间线 + 单步重试 + 预览页码跳转。
 * P2 增强：资产编辑器（名称/标签/分类/按空间 schema 动态元数据）+ 审核状态 + 图片体验
 * （renditions 缩略图 / 原图预览 / EXIF 信息面板）。
 */

const JOB_TYPE_LABEL: Record<string, string> = {
  parse: '解析',
  chunk: 'LLM 分片+抽取',
  graph: '图谱写入',
  embed: '嵌入',
  index: '索引',
  transcode: '懒转码',
};

const JOB_STATUS_TONE: Record<string, 'gray' | 'blue' | 'green' | 'red'> = {
  pending: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
};

/** 任务记录表每页条数（与旧版一致）。 */
const JOB_PAGE_SIZE = 8;

/** 分类下拉/元数据下拉的「未设置」哨兵值（Radix Select 不允许空串 value）。 */
const NONE = '__unset__';

const editorSchema = z.object({
  name: z.string().min(1, '请输入名称'),
  tags: z.array(z.string()),
  category: z.string(), // '' = 无分类（unset）
  metadata: z.record(z.string(), z.any()),
});

type EditorValues = z.infer<typeof editorSchema>;

/** 空间元数据 schema 字段（P2-CORE-04）。 */
interface MetadataField {
  id: string;
  key: string;
  name: string;
  field_type: string;
  required: boolean;
  options: unknown[];
}

export function AssetDetailPage() {
  const { assetId } = useParams<{ assetId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const form = useForm<EditorValues>({
    resolver: zodResolver(editorSchema),
    defaultValues: { name: '', tags: [], category: '', metadata: {} },
  });
  const [page, setPage] = useState<number | undefined>(
    searchParams.get('page') ? Number(searchParams.get('page')) : undefined,
  );
  // P3-WEB-02/03：检索命中视频/音频单元时经 ?t=<秒> seek 到时间点
  const seekTo = searchParams.get('t') ? Number(searchParams.get('t')) : undefined;
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [tagDraft, setTagDraft] = useState('');
  const [viewing, setViewing] = useState<string | null>(null);
  const [jobPage, setJobPage] = useState(1);

  const asset = useQuery({
    queryKey: ['asset', assetId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载资产失败'));
      return data;
    },
    refetchInterval: (q) =>
      q.state.data && ['pending', 'processing'].includes(q.state.data.status) ? 3000 : false,
  });

  const jobs = useQuery({
    queryKey: ['asset-jobs', assetId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets/{asset_id}/jobs', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载任务失败'));
      return data;
    },
  });

  const preview = useQuery({
    queryKey: ['asset-preview', assetId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets/{asset_id}/preview', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载预览失败'));
      return data;
    },
  });

  const spaceId = asset.data?.space_id ?? undefined;

  const space = useQuery({
    queryKey: ['space', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载空间失败'));
      return data;
    },
  });

  const categories = useSpaceCategories(spaceId);

  const metadataFields = useQuery({
    queryKey: ['space-metadata-fields', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/metadata-fields', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载元数据 schema 失败'));
      return data;
    },
  });

  useEffect(() => {
    if (preview.data?.mode === 'markdown') {
      if (preview.data.content) setMarkdown(preview.data.content);
      else if (preview.data.url) void fetch(preview.data.url).then((r) => r.text()).then(setMarkdown);
    }
  }, [preview.data]);

  const retry = useMutation({
    mutationFn: async (step?: string) => {
      const { error } = await api.POST('/api/v1/assets/{asset_id}/retry', {
        params: { path: { asset_id: assetId! } },
        body: { step },
      });
      if (error) throw new Error(extractApiError(error, '重试失败'));
    },
    onSuccess: () => {
      toast.success('已重新入队');
      void asset.refetch();
      void jobs.refetch();
    },
    onError: (e) => toast.error(e.message),
  });

  // ---- 编辑器 ----
  const a = asset.data;
  const isMedia = ['video/', 'audio/'].some((p) => (a?.mime_type ?? '').startsWith(p));
  const myRole = space.data?.my_role;
  const canEdit = myRole !== 'viewer'; // 未知（空间加载失败）时放开，由后端兜底

  useEffect(() => {
    if (!a) return;
    form.setValue('name', a.name);
    form.setValue('tags', a.tags.map((t) => t.name));
    form.setValue('category', a.category_id ?? '');
  }, [a, form]);

  const save = useMutation({
    mutationFn: async (values: EditorValues) => {
      const metaRaw = values.metadata ?? {};
      // 元数据按 schema 类型归一化：date → ISO 日期字符串，number → 数值
      const metadata: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(metaRaw)) {
        if (v === undefined || v === null || v === '' || (typeof v === 'number' && Number.isNaN(v)))
          continue;
        metadata[k] = v;
      }
      const { error } = await api.PATCH('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId! } },
        body: {
          name: values.name,
          tags: values.tags ?? [],
          category_id: values.category ? values.category : undefined,
          unset_category: !values.category,
          metadata,
        },
      });
      if (error) throw new Error(extractApiError(error, '保存失败'));
    },
    onSuccess: () => {
      toast.success('资产已更新');
      void queryClient.invalidateQueries({ queryKey: ['asset', assetId] });
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, '删除失败'));
    },
    onSuccess: () => {
      toast.success('已删除（含向量清理）');
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
      navigate(spaceId ? `/s/${spaceId}/assets` : '/assets');
    },
    onError: (e) => toast.error(e.message),
  });

  // 元数据控件清单：空间 schema 字段 + 现有元数据中的额外键（保数据不丢）
  const metaKeys = useMemo(() => {
    const schemaKeys = (metadataFields.data ?? []).map((f) => f.key);
    const extraKeys = Object.keys(a?.metadata ?? {}).filter((k) => !schemaKeys.includes(k));
    return [...schemaKeys, ...extraKeys];
  }, [metadataFields.data, a?.metadata]);
  const fieldByKey = useMemo(() => {
    const m = new Map<string, MetadataField>();
    for (const f of metadataFields.data ?? []) m.set(f.key, f);
    return m;
  }, [metadataFields.data]);

  // 将现有元数据值灌入表单（schema 异步加载完成后按 metaKeys 灌值）
  useEffect(() => {
    if (!a) return;
    for (const k of metaKeys) {
      const v = a.metadata[k];
      if (v !== undefined && v !== null) form.setValue(`metadata.${k}`, v as string | number);
    }
  }, [a, metaKeys, form]);

  const tagsValue = form.watch('tags');
  const metaWatch = form.watch('metadata');

  /** 标签输入回车/逗号成词（对应旧版 Select mode="tags" + tokenSeparators）。 */
  const commitTag = () => {
    const parts = tagDraft.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const next = [...tagsValue];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    if (next.length !== tagsValue.length) form.setValue('tags', next);
    setTagDraft('');
  };

  const onSubmit = form.handleSubmit((values) => {
    // 必填元数据校验（动态 schema 不进 zod），文案与旧版 rules 一致
    for (const key of metaKeys) {
      const field = fieldByKey.get(key);
      if (!field?.required) continue;
      const v = values.metadata[key];
      if (v === undefined || v === null || v === '') {
        toast.error(`请填写 ${field.name}`);
        return;
      }
    }
    save.mutate(values);
  });

  if (asset.isLoading)
    return (
      <Card>
        <CardContent className="space-y-3">
          <Skeleton className="h-6 w-1/3" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-20 w-full" />
        </CardContent>
      </Card>
    );
  if (asset.isError)
    return (
      <Card>
        <CardContent>
          <p className="text-sm text-destructive">{(asset.error as Error).message}</p>
        </CardContent>
      </Card>
    );
  if (!a)
    return (
      <Card>
        <CardContent className="text-sm text-muted-foreground">资产不存在</CardContent>
      </Card>
    );

  const latestByStep = new Map<string, NonNullable<typeof jobs.data>[number]>();
  for (const j of jobs.data ?? []) {
    if (!latestByStep.has(j.job_type)) latestByStep.set(j.job_type, j);
  }
  const stepStatus = (step: string): 'wait' | 'finish' | 'error' | 'process' => {
    const j = latestByStep.get(step);
    if (!j) return 'wait';
    if (j.status === 'succeeded') return 'finish';
    if (j.status === 'failed') return 'error';
    return 'process';
  };
  const failedStep = [...latestByStep.values()].find((j) => j.status === 'failed')?.job_type;

  // 图片体验：缩略图 renditions + 原图 + EXIF
  const exif = (a.version_meta?.parse as Record<string, unknown> | undefined)?.exif as
    | Record<string, unknown>
    | undefined;
  const caption = (a.asset_meta as Record<string, unknown> | undefined)?.caption;
  const thumbnails = a.renditions
    .filter((r) => r.kind === 'thumbnail' && r.url)
    .map((r) => ({ url: r.url as string, width: r.width, height: r.height }));
  const mainImageUrl = preview.data?.mode === 'image' ? (preview.data.url ?? null) : null;

  const jobList = jobs.data ?? [];
  type JobRow = NonNullable<typeof jobs.data>[number];
  const jobColumns: ColumnDef<JobRow, unknown>[] = [
    {
      accessorKey: 'job_type',
      header: '步骤',
      cell: ({ row }) => (
        <span>{JOB_TYPE_LABEL[row.original.job_type] ?? row.original.job_type}</span>
      ),
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => (
        <StatusBadge tone={JOB_STATUS_TONE[row.original.status] ?? 'gray'}>
          {row.original.status}
        </StatusBadge>
      ),
    },
    {
      accessorKey: 'progress',
      header: '进度',
      cell: ({ row }) => <span>{Math.round(row.original.progress * 100)}%</span>,
    },
    { accessorKey: 'attempts', header: '尝试' },
    {
      id: 'duration',
      header: '耗时',
      cell: ({ row }) => (
        <span>
          {row.original.started_at && row.original.finished_at
            ? `${((new Date(row.original.finished_at).getTime() - new Date(row.original.started_at).getTime()) / 1000).toFixed(1)}s`
            : '-'}
        </span>
      ),
    },
    {
      accessorKey: 'error',
      header: '错误',
      cell: ({ row }) =>
        row.original.error ? (
          <span className="block max-w-48 truncate text-destructive" title={row.original.error}>
            {row.original.error}
          </span>
        ) : (
          <span>-</span>
        ),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <Button variant="outline" size="xs" onClick={() => retry.mutate(row.original.job_type)}>
          重跑此步
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base break-all">{a.name}</CardTitle>
          <CardAction>
            <div className="flex flex-wrap items-center gap-2">
              {a.status === 'failed' && (
                <Button size="sm" onClick={() => retry.mutate(failedStep)}>
                  从失败步骤重跑{failedStep ? `（${JOB_TYPE_LABEL[failedStep]}）` : ''}
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  void asset.refetch();
                  void jobs.refetch();
                }}
              >
                <RefreshCw /> 刷新
              </Button>
              <ConfirmAction
                trigger={
                  <Button variant="destructive" size="sm" disabled={remove.isPending}>
                    <Trash2 /> 删除
                  </Button>
                }
                title="删除资产？"
                description="将级联清理语义单元与向量，不可恢复。"
                confirmText="删除"
                danger
                onConfirm={() => remove.mutate()}
              />
            </div>
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-4">
          <DescriptionList cols={3}>
            <DescriptionItem label="类型">{a.mime_type}</DescriptionItem>
            <DescriptionItem label="大小">{(a.size_bytes / 1024).toFixed(1)} KB</DescriptionItem>
            <DescriptionItem label="页数">{a.page_count ?? '-'}</DescriptionItem>
            <DescriptionItem label="状态">
              <StatusBadge tone={a.status === 'ready' ? 'green' : a.status === 'failed' ? 'red' : 'blue'}>
                {a.status}
              </StatusBadge>
            </DescriptionItem>
            <DescriptionItem label="审核">
              <span className="flex flex-wrap items-center gap-2">
                <ReviewStatusTag status={a.review_status} />
                {a.review_reason && <span className="text-xs text-amber-600">{a.review_reason}</span>}
              </span>
            </DescriptionItem>
            <DescriptionItem label="分片方式">{a.chunk_method ?? '-'}</DescriptionItem>
            <DescriptionItem label="创建时间">{new Date(a.created_at).toLocaleString()}</DescriptionItem>
            <DescriptionItem label="标签">
              {a.tags.length > 0 ? (
                <span className="flex flex-wrap gap-1">
                  {a.tags.map((t) => (
                    <StatusBadge key={t.id} tone="blue">
                      {t.name}
                    </StatusBadge>
                  ))}
                </span>
              ) : null}
            </DescriptionItem>
            <DescriptionItem label="上传者">{a.created_by ?? '-'}</DescriptionItem>
          </DescriptionList>
          {a.status_reason && <p className="text-sm text-destructive">失败原因：{a.status_reason}</p>}
          <StepTimeline
            steps={['parse', 'chunk', 'embed', 'index'].map((step) => ({
              key: step,
              label: JOB_TYPE_LABEL[step],
              state: stepStatus(step),
              description: latestByStep.get(step)?.error?.slice(0, 80),
            }))}
          />
        </CardContent>
      </Card>

      {/* 图片体验：缩略图网格 / 原图查看 / EXIF 面板 */}
      {a.is_image && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">图片</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-3">
              {mainImageUrl && (
                <button
                  type="button"
                  className="overflow-hidden rounded-md border"
                  onClick={() => setViewing(mainImageUrl)}
                >
                  <img src={mainImageUrl} alt={a.name} className="w-80 object-cover" />
                </button>
              )}
              {thumbnails.map((r) => (
                <button
                  key={r.url}
                  type="button"
                  className="overflow-hidden rounded-md border"
                  onClick={() => setViewing(r.url)}
                >
                  <img
                    src={r.url}
                    alt={`thumbnail ${r.width ?? ''}x${r.height ?? ''}`}
                    className="w-40 object-cover"
                  />
                </button>
              ))}
              {!mainImageUrl && thumbnails.length === 0 && (
                <p className="text-sm text-muted-foreground">暂无可用图片。</p>
              )}
            </div>
            {caption ? (
              <p className="text-sm text-muted-foreground">图片描述：{String(caption)}</p>
            ) : null}
            {exif && Object.keys(exif).length > 0 && (
              <>
                <div className="flex items-center gap-3 pt-2">
                  <span className="text-sm text-muted-foreground">EXIF 信息</span>
                  <Separator className="flex-1" />
                </div>
                <DescriptionList cols={3}>
                  {Object.entries(exif).map(([k, v]) => (
                    <DescriptionItem key={k} label={k}>
                      {String(v)}
                    </DescriptionItem>
                  ))}
                </DescriptionList>
              </>
            )}
            {thumbnails.some((r) => r.width || r.height) && (
              <p className="text-sm text-muted-foreground">
                缩略图尺寸：
                {thumbnails.map((r) => `${r.width ?? '?'}×${r.height ?? '?'}`).join('、')}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* 编辑器（editor+ 可用；viewer 只读） */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Pencil className="size-4" /> 编辑
            {!canEdit && <BadgeGhost>viewer 只读</BadgeGhost>}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="max-w-[640px] space-y-4" noValidate>
            <div className="space-y-2">
              <Label htmlFor="asset-name">名称</Label>
              <Input id="asset-name" disabled={!canEdit} {...form.register('name')} />
              {form.formState.errors.name && (
                <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="asset-tags">标签</Label>
              <Input
                id="asset-tags"
                value={tagDraft}
                disabled={!canEdit}
                placeholder="如：合同 / 2026 / 重要"
                onChange={(e) => setTagDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    commitTag();
                  } else if (e.key === 'Backspace' && tagDraft === '' && tagsValue.length > 0) {
                    form.setValue('tags', tagsValue.slice(0, -1));
                  }
                }}
                onBlur={commitTag}
              />
              {tagsValue.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {tagsValue.map((t, i) => (
                    <span
                      key={`${t}-${i}`}
                      className="inline-flex items-center gap-1 rounded-md border bg-secondary px-1.5 py-0.5 text-xs text-secondary-foreground"
                    >
                      {t}
                      {canEdit && (
                        <button
                          type="button"
                          aria-label={`移除标签 ${t}`}
                          className="text-muted-foreground hover:text-foreground"
                          onClick={() =>
                            form.setValue('tags', tagsValue.filter((_, j) => j !== i))
                          }
                        >
                          <X className="size-3" />
                        </button>
                      )}
                    </span>
                  ))}
                </div>
              )}
              <p className="text-xs text-muted-foreground">输入后回车创建，保存时整体替换标签</p>
            </div>
            <div className="space-y-2">
              <Label>分类</Label>
              <Select
                value={form.watch('category') || NONE}
                onValueChange={(v) => form.setValue('category', v === NONE ? '' : v)}
                disabled={!canEdit}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="选择分类" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>无分类</SelectItem>
                  {(categories.data ?? []).map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {metaKeys.length > 0 && (
              <>
                <div className="flex items-center gap-3 pt-2">
                  <span className="text-sm text-muted-foreground">元数据</span>
                  <Separator className="flex-1" />
                </div>
                {metaKeys.map((key) => {
                  const field = fieldByKey.get(key);
                  const label = field?.name ?? key;
                  const required = field?.required ?? false;
                  const type = field?.field_type ?? 'text';
                  const raw = metaWatch?.[key];
                  return (
                    <div key={key} className="space-y-2">
                      <Label htmlFor={`meta-${key}`}>
                        {label}
                        {field ? `（${key}）` : ''}
                        {required && <span className="ml-0.5 text-destructive">*</span>}
                      </Label>
                      {type === 'number' ? (
                        <Input
                          id={`meta-${key}`}
                          type="number"
                          step="any"
                          className="w-[200px]"
                          disabled={!canEdit}
                          {...form.register(`metadata.${key}`, { valueAsNumber: true })}
                        />
                      ) : type === 'date' ? (
                        <Input
                          id={`meta-${key}`}
                          type="date"
                          className="w-[200px]"
                          disabled={!canEdit}
                          {...form.register(`metadata.${key}`)}
                        />
                      ) : type === 'select' ? (
                        <Select
                          value={
                            raw === undefined || raw === null || raw === '' ? NONE : String(raw)
                          }
                          onValueChange={(v) =>
                            form.setValue(
                              `metadata.${key}`,
                              v === NONE ? undefined : (v as string | number),
                            )
                          }
                          disabled={!canEdit}
                        >
                          <SelectTrigger className="w-[200px]">
                            <SelectValue placeholder="请选择" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={NONE}>未设置</SelectItem>
                            {(field?.options ?? []).map((o) => (
                              <SelectItem key={String(o)} value={String(o)}>
                                {String(o)}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      ) : (
                        <Input
                          id={`meta-${key}`}
                          className="w-full"
                          disabled={!canEdit}
                          {...form.register(`metadata.${key}`)}
                        />
                      )}
                    </div>
                  );
                })}
              </>
            )}

            {canEdit && (
              <Button type="submit" disabled={save.isPending}>
                {save.isPending ? '保存中…' : '保存修改'}
              </Button>
            )}
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">预览定位</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {isMedia && <MediaPlayer assetId={assetId!} seekTo={seekTo} />}
          {preview.data?.mode === 'pdf' && (
            <>
              {preview.data.page_count ? (
                <div className="flex flex-wrap gap-1.5">
                  {[...Array(preview.data.page_count)].map((_, i) => (
                    <Button
                      key={i}
                      size="sm"
                      variant={page === i + 1 ? 'default' : 'outline'}
                      onClick={() => setPage(i + 1)}
                    >
                      第 {i + 1} 页
                    </Button>
                  ))}
                </div>
              ) : null}
              <iframe
                key={page}
                src={`${preview.data.url}${page ? `#page=${page}` : ''}`}
                className="h-[640px] w-full rounded-md border"
                title="PDF 预览"
              />
            </>
          )}
          {preview.data?.mode === 'markdown' && markdown !== null && (
            <pre className="max-h-[640px] overflow-auto whitespace-pre-wrap text-sm">
              {markdown}
            </pre>
          )}
          {!preview.data && <p className="text-sm text-muted-foreground">解析完成后可预览。</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">任务记录</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={jobColumns}
            data={jobList.slice((jobPage - 1) * JOB_PAGE_SIZE, jobPage * JOB_PAGE_SIZE)}
            loading={jobs.isLoading}
            total={jobList.length}
            page={jobPage}
            pageSize={JOB_PAGE_SIZE}
            onPageChange={setJobPage}
            getRowId={(row) => row.id}
          />
        </CardContent>
      </Card>

      {/* 原图查看（替代 antd Image.PreviewGroup 预览） */}
      <Dialog open={viewing !== null} onOpenChange={(o) => !o && setViewing(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{a.name}</DialogTitle>
          </DialogHeader>
          {viewing && <img src={viewing} alt={a.name} className="max-h-[75vh] w-full object-contain" />}
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** 无色徽标（对应旧版默认 Tag）。 */
function BadgeGhost(props: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-md border px-1.5 py-0.5 text-xs font-normal text-muted-foreground">
      {props.children}
    </span>
  );
}

/** 处理步骤时间线（替代 antd Steps）：parse → chunk → embed → index。 */
function StepTimeline(props: {
  steps: { key: string; label: string; state: 'wait' | 'process' | 'finish' | 'error'; description?: string }[];
}) {
  return (
    <ol className="flex">
      {props.steps.map((s, i) => (
        <li key={s.key} className="min-w-0 flex-1">
          <div className="flex items-center">
            <span
              className={cn(
                'flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-medium',
                s.state === 'finish' && 'bg-primary text-primary-foreground',
                s.state === 'process' && 'border border-primary text-primary',
                s.state === 'error' && 'bg-destructive text-white',
                s.state === 'wait' && 'border bg-muted text-muted-foreground',
              )}
            >
              {s.state === 'error' ? <X className="size-3.5" /> : i + 1}
            </span>
            {i < props.steps.length - 1 && (
              <span
                className={cn(
                  'h-px flex-1',
                  s.state === 'finish'
                    ? 'bg-primary'
                    : s.state === 'error'
                      ? 'bg-destructive'
                      : 'bg-border',
                )}
              />
            )}
          </div>
          <p className={cn('mt-1.5 text-sm', s.state === 'wait' ? 'text-muted-foreground' : 'font-medium')}>
            {s.label}
          </p>
          {s.description && (
            <p className="truncate text-xs text-muted-foreground" title={s.description}>
              {s.description}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}
