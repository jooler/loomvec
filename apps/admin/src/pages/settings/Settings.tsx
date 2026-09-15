import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import { Switch } from '@loomvec/ui/components/ui/switch';
import { Textarea } from '@loomvec/ui/components/ui/textarea';
import type { SettingItem } from '@/types';

const GROUP_TITLE: Record<string, string> = {
  ai: 'AI 供方配置',
  retrieval: '检索参数',
  upload: '上传策略',
  sso: 'SSO / OIDC',
  extensions: '扩展插件',
};

/** 编辑值控件：按原始值类型选择（布尔 → 开关；数字 → 输入框；字符串 → 纯文本；
 *  数组/对象 → JSON 文本）。仅结构化（数组/对象）值尝试 JSON.parse，
 *  避免字符串值被静默强转（"007"→7、"true"→true、"null"→null）。 */
function ValueEditor(props: {
  value: unknown;
  /** 原始值是否为数组/对象（决定文本按 JSON 解析还是按纯字符串提交） */
  structured: boolean;
  onChange: (v: unknown) => void;
}) {
  const { value } = props;
  if (typeof value === 'boolean') {
    return <Switch checked={value} onCheckedChange={(v) => props.onChange(v)} />;
  }
  if (typeof value === 'number') {
    return (
      <Input
        type="number"
        className="w-60"
        value={value}
        onChange={(e) => props.onChange(e.target.value === '' ? 0 : Number(e.target.value))}
      />
    );
  }
  return (
    <Textarea
      rows={3}
      className="max-w-md"
      value={typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
      onChange={(e) => {
        const text = e.target.value;
        if (!props.structured) {
          props.onChange(text);
          return;
        }
        // 结构化值：尽量解析为 JSON 以便保存对象/数组；半成品输入先按原文本暂存
        try {
          props.onChange(JSON.parse(text));
        } catch {
          props.onChange(text);
        }
      }}
    />
  );
}

/** 配置页（五个路由复用，按 group 过滤 GET /admin/settings 渲染，docs/04 §5.9）。 */
export function SettingsPage({ group }: { group: string }) {
  const { isSuperAdmin } = usePerm();
  const queryClient = useQueryClient();
  const [editTarget, setEditTarget] = useState<SettingItem | null>(null);
  const [editValue, setEditValue] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['admin-settings'],
    queryFn: () => unwrap<SettingItem[]>(api.GET('/api/v1/admin/settings')),
  });

  const items = (data ?? []).filter((s) => s.group === group);

  const save = async (reason: string) => {
    if (!editTarget) return;
    // 结构化配置：半成品 JSON（解析失败暂存的字符串）不允许提交
    if (
      editTarget.value != null &&
      typeof editTarget.value === 'object' &&
      typeof editValue === 'string'
    ) {
      toast.error('JSON 格式无效，请修正后再保存');
      return;
    }
    setBusy(true);
    try {
      await unwrap(
        api.PUT('/api/v1/admin/settings/{key}', {
          params: { path: { key: editTarget.key } },
          body: { value: editValue, reason },
        }),
      );
      toast.success('配置已保存');
      setEditTarget(null);
      void queryClient.invalidateQueries({ queryKey: ['admin-settings'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<SettingItem, unknown>[] = [
    {
      accessorKey: 'key',
      header: '配置键',
      cell: ({ row }) => (
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{row.original.key}</code>
      ),
    },
    { accessorKey: 'description', header: '说明' },
    {
      accessorKey: 'value',
      header: '当前值',
      cell: ({ row }) => {
        const v = row.original.value;
        return row.original.sensitive ? (
          <span className="text-sm text-muted-foreground">{String(v ?? '未设置')}</span>
        ) : (
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
            {typeof v === 'object' ? JSON.stringify(v) : String(v ?? '-')}
          </code>
        );
      },
    },
    {
      accessorKey: 'effect',
      header: '生效方式',
      cell: ({ row }) =>
        row.original.effect === 'restart' ? (
          <StatusBadge tone="amber">需重启</StatusBadge>
        ) : (
          <StatusBadge tone="green">即时生效</StatusBadge>
        ),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => {
        const r = row.original;
        return r.sensitive && r.value == null ? (
          <Button
            variant="link"
            size="sm"
            disabled={!isSuperAdmin}
            onClick={() => {
              setEditTarget(r);
              setEditValue('');
            }}
          >
            设置
          </Button>
        ) : (
          <Button
            variant="link"
            size="sm"
            disabled={!isSuperAdmin}
            title={isSuperAdmin ? undefined : '仅 super_admin 可修改系统配置'}
            onClick={() => {
              setEditTarget(r);
              setEditValue(r.value);
            }}
          >
            编辑
          </Button>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={GROUP_TITLE[group] ?? group}
        description={
          group === 'ai' || group === 'sso' || group === 'extensions'
            ? '本组仅 super_admin 可修改'
            : undefined
        }
      />

      <DataTable
        columns={columns}
        data={items}
        loading={isLoading}
        error={isError ? error : undefined}
      />

      {/* 配置修改：必填理由入审计（docs/04 §六） */}
      <ReasonModal
        open={editTarget !== null}
        title={`修改配置：${editTarget?.key ?? ''}`}
        description={
          editTarget?.effect === 'restart'
            ? '该配置需重启服务后生效。'
            : editTarget?.sensitive
              ? '敏感配置写入后仅回显脱敏值。'
              : undefined
        }
        okText="保存"
        confirmLoading={busy}
        onCancel={() => setEditTarget(null)}
        onOk={save}
      >
        {editTarget && (
          <div className="space-y-2 pb-4">
            <p className="text-sm font-medium">新值（{editTarget.description}）</p>
            <ValueEditor
              value={editValue}
              structured={editTarget.value != null && typeof editTarget.value === 'object'}
              onChange={setEditValue}
            />
          </div>
        )}
      </ReasonModal>
    </div>
  );
}
