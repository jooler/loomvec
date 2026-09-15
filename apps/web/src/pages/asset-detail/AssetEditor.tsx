import { useEffect, useMemo, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Pencil, X } from 'lucide-react';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { extractApiError } from '@/utils';
import { t as sharedT } from '@/i18n';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
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
import { NONE } from './constants';
import type { AssetDetailData } from './use-asset-queries';

/** 空间元数据 schema 字段（P2-CORE-04）。 */
interface MetadataField {
  id: string;
  key: string;
  name: string;
  field_type: string;
  required: boolean;
  options: unknown[];
}

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const editorSchema = z.object({
  name: z.string().min(1, sharedT('assetDetail:nameRequired')),
  tags: z.array(z.string()),
  category: z.string(), // '' = 无分类（unset）
  metadata: z.record(z.string(), z.any()),
});

type EditorValues = z.infer<typeof editorSchema>;

/** 资产编辑器（P2：名称/标签/分类/按空间 schema 动态元数据）；viewer 只读。 */
export function AssetEditor(props: {
  asset: AssetDetailData;
  categories: { id: string; name: string }[];
  metadataFields: MetadataField[];
  canEdit: boolean;
}) {
  const { asset: a, canEdit } = props;
  const queryClient = useQueryClient();
  const { t } = useTranslation('assetDetail');
  const form = useForm<EditorValues>({
    resolver: zodResolver(editorSchema),
    defaultValues: { name: '', tags: [], category: '', metadata: {} },
  });

  useEffect(() => {
    form.setValue('name', a.name);
    form.setValue('tags', a.tags.map((t) => t.name));
    form.setValue('category', a.category_id ?? '');
  }, [a, form]);

  // 元数据控件清单：空间 schema 字段 + 现有元数据中的额外键（保数据不丢）
  const metaKeys = useMemo(() => {
    const schemaKeys = props.metadataFields.map((f) => f.key);
    const extraKeys = Object.keys(a.metadata ?? {}).filter((k) => !schemaKeys.includes(k));
    return [...schemaKeys, ...extraKeys];
  }, [props.metadataFields, a.metadata]);
  const fieldByKey = useMemo(() => {
    const m = new Map<string, MetadataField>();
    for (const f of props.metadataFields) m.set(f.key, f);
    return m;
  }, [props.metadataFields]);

  // 将现有元数据值灌入表单（schema 异步加载完成后按 metaKeys 灌值）
  useEffect(() => {
    for (const k of metaKeys) {
      const v = a.metadata[k];
      if (v !== undefined && v !== null) form.setValue(`metadata.${k}`, v as string | number);
    }
  }, [a, metaKeys, form]);

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
        params: { path: { asset_id: a.id } },
        body: {
          name: values.name,
          tags: values.tags ?? [],
          category_id: values.category ? values.category : undefined,
          unset_category: !values.category,
          unset_folder: false,
          metadata,
        },
      });
      if (error) throw new Error(extractApiError(error, t('editor.saveFailed')));
    },
    onSuccess: () => {
      toast.success(t('editor.saved'));
      void queryClient.invalidateQueries({ queryKey: ['asset', a.id] });
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
    },
    onError: (e) => toast.error(e.message),
  });

  const tagsValue = form.watch('tags');
  const categoryValue = form.watch('category');
  const metaWatch = form.watch('metadata');
  const [tagDraft, setTagDraft] = useState('');

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
        toast.error(t('editor.metaRequired', { name: field.name }));
        return;
      }
    }
    save.mutate(values);
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Pencil className="size-4" /> {t('action.edit')}
          {!canEdit && <BadgeGhost>{t('editor.viewerReadonly')}</BadgeGhost>}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="max-w-[640px] space-y-4" noValidate>
          <div className="space-y-2">
            <Label htmlFor="asset-name">{t('field.name')}</Label>
            <Input id="asset-name" disabled={!canEdit} {...form.register('name')} />
            {form.formState.errors.name && (
              <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="asset-tags">{t('tagsLabel')}</Label>
            <Input
              id="asset-tags"
              value={tagDraft}
              disabled={!canEdit}
              placeholder={t('editor.tagsPlaceholder')}
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
                {tagsValue.map((tag, i) => (
                  <span
                    key={`${tag}-${i}`}
                    className="inline-flex items-center gap-1 rounded-md border bg-secondary px-1.5 py-0.5 text-xs text-secondary-foreground"
                  >
                    {tag}
                    {canEdit && (
                      <button
                        type="button"
                        aria-label={t('editor.removeTag', { name: tag })}
                        className="text-muted-foreground hover:text-foreground"
                        onClick={() => form.setValue('tags', tagsValue.filter((_, j) => j !== i))}
                      >
                        <X className="size-3" />
                      </button>
                    )}
                  </span>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground">{t('editor.tagsHint')}</p>
          </div>
          <div className="space-y-2">
            <Label>{t('editor.categoryLabel')}</Label>
            <Select
              value={categoryValue || NONE}
              onValueChange={(v) => form.setValue('category', v === NONE ? '' : v)}
              disabled={!canEdit}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder={t('editor.selectCategoryPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>{t('editor.noCategory')}</SelectItem>
                {props.categories.map((c) => (
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
                <span className="text-sm text-muted-foreground">{t('editor.metadataLabel')}</span>
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
                          <SelectValue placeholder={t('editor.selectPlaceholder')} />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={NONE}>{t('editor.unsetOption')}</SelectItem>
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
              {save.isPending ? t('action.saving') : t('editor.saveChanges')}
            </Button>
          )}
        </form>
      </CardContent>
    </Card>
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
