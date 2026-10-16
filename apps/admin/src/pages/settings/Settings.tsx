import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { t as tt } from '@/i18n';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { Switch } from '@loomvec/ui/components/ui/switch';
import { Textarea } from '@loomvec/ui/components/ui/textarea';
import type { SettingItem } from '@/types';
import { AgentGatewayCard } from './AgentGatewayCard';
import { AiProvidersForm } from './AiProvidersForm';

/** 枚举型配置键 → 可选值（用下拉而不是自由文本）。 */
const ENUM_OPTIONS: Record<string, readonly string[]> = {
  'agent.default_agent': ['claude', 'dsh'],
};

// 模块级文案（分组标题字典）：main.tsx 已先初始化 i18n，import 阶段取值安全
const GROUP_TITLE: Record<string, string> = {
  ai: tt('settings:group.ai'),
  agent: tt('settings:group.agent'),
  retrieval: tt('settings:group.retrieval'),
  pipeline: tt('settings:group.pipeline'),
  upload: tt('settings:group.upload'),
  sso: tt('settings:group.sso'),
  extensions: tt('settings:group.extensions'),
};

/** 编辑值控件：按原始值类型选择（布尔 → 开关；数字 → 输入框；字符串 → 纯文本；
 *  数组/对象 → JSON 文本）。仅结构化（数组/对象）值尝试 JSON.parse，
 *  避免字符串值被静默强转（"007"→7、"true"→true、"null"→null）。 */
function ValueEditor(props: {
  settingKey: string;
  value: unknown;
  /** 原始值是否为数组/对象（决定文本按 JSON 解析还是按纯字符串提交） */
  structured: boolean;
  onChange: (v: unknown) => void;
}) {
  const { value } = props;
  const options = ENUM_OPTIONS[props.settingKey];
  if (options) {
    return (
      <Select value={String(value ?? '')} onValueChange={(v) => props.onChange(v)}>
        <SelectTrigger className="w-60">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o} value={o}>
              {o}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
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
  // 结构化值（如自定义抽取领域配置）可能较长：给更大的等宽编辑区
  return (
    <Textarea
      rows={props.structured ? 14 : 3}
      className={props.structured ? 'max-w-2xl font-mono text-xs' : 'max-w-md'}
      value={
        typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2)
      }
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

/** 配置页（路由复用，按 group 过滤 GET /admin/settings 渲染，docs/04 §5.9）。
 * ai 组为分层表单（每通道本地 GPU / 云端，docs/16），其余组为通用键值表。 */
export function SettingsPage({ group }: { group: string }) {
  const { isSuperAdmin } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('settings');
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
      toast.error(t('jsonInvalid'));
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
      toast.success(t('savedToast'));
      setEditTarget(null);
      void queryClient.invalidateQueries({ queryKey: ['admin-settings'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<SettingItem, unknown>[] = [
    {
      accessorKey: 'key',
      header: t('col.key'),
      cell: ({ row }) => (
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{row.original.key}</code>
      ),
    },
    { accessorKey: 'description', header: t('col.description') },
    {
      accessorKey: 'value',
      header: t('col.value'),
      cell: ({ row }) => {
        const v = row.original.value;
        return row.original.sensitive ? (
          <span className="text-sm text-muted-foreground">{String(v ?? t('notSet'))}</span>
        ) : (
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
            {typeof v === 'object' ? JSON.stringify(v) : String(v ?? '-')}
          </code>
        );
      },
    },
    {
      accessorKey: 'effect',
      header: t('col.effect'),
      cell: ({ row }) =>
        row.original.effect === 'restart' ? (
          <StatusBadge tone="amber">{t('effect.restart')}</StatusBadge>
        ) : (
          <StatusBadge tone="green">{t('effect.immediate')}</StatusBadge>
        ),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => {
        const r = row.original;
        // 敏感项只写不读：回显的是脱敏占位，编辑框从空开始（保存空值 = 清除）
        const unset = r.value == null;
        return (
          <Button
            variant="link"
            size="sm"
            disabled={!isSuperAdmin}
            title={isSuperAdmin ? undefined : t('superAdminOnlyHint')}
            onClick={() => {
              setEditTarget(r);
              setEditValue(r.sensitive || unset ? '' : r.value);
            }}
          >
            {r.sensitive && unset ? t('set') : t('action.edit')}
          </Button>
        );
      },
    },
  ];

  if (group === 'ai') {
    return (
      <div className="space-y-4">
        <PageHeader title={GROUP_TITLE[group] ?? group} description={t('superAdminOnly')} />
        {isLoading ? (
          <p className="text-sm text-muted-foreground">{t('loading')}</p>
        ) : isError ? (
          <p className="text-sm text-destructive">{(error as Error).message}</p>
        ) : (
          <AiProvidersForm items={items} />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title={GROUP_TITLE[group] ?? group}
        description={
          group === 'sso' ||
          group === 'extensions' ||
          group === 'agent' ||
          group === 'pipeline'
            ? t('superAdminOnly')
            : undefined
        }
      />

      {group === 'agent' && <AgentGatewayCard />}

      <DataTable
        columns={columns}
        data={items}
        getRowId={(r) => r.key}
        loading={isLoading}
        error={isError ? error : undefined}
      />

      {/* 配置修改：必填理由入审计（docs/04 §六） */}
      <ReasonModal
        open={editTarget !== null}
        title={t('editTitle', { key: editTarget?.key ?? '' })}
        description={
          editTarget?.effect === 'restart'
            ? t('restartDescription')
            : editTarget?.sensitive
              ? t('sensitiveDescription')
              : undefined
        }
        okText={t('action.save')}
        confirmLoading={busy}
        onCancel={() => setEditTarget(null)}
        onOk={save}
      >
        {editTarget && (
          <div className="space-y-2 pb-4">
            <p className="text-sm font-medium">
              {t('newValue', { description: editTarget.description })}
            </p>
            <ValueEditor
              settingKey={editTarget.key}
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
