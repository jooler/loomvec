import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { formatBytes, formatDateTime } from '@/utils';
import type { PagedResp, SpaceRow } from '@/types';

const PAGE_SIZE = 20;

/** 空间治理列表：全量空间（含个人空间）+ 类型/封禁筛选（docs/04 §5.4）。 */
export function SpaceListPage() {
  const navigate = useNavigate();
  const { t } = useTranslation('spaces');
  const [q, setQ] = useState('');
  const [draftQ, setDraftQ] = useState('');
  const [spaceType, setSpaceType] = useState<string | undefined>();
  const [banned, setBanned] = useState<string | undefined>();
  const [page, setPage] = useState(1);

  const limit = PAGE_SIZE;
  const offset = (page - 1) * PAGE_SIZE;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-spaces', q, spaceType, banned, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<SpaceRow>>(
        api.GET('/api/v1/admin/spaces', {
          params: {
            query: {
              q: q || undefined,
              space_type: spaceType || undefined,
              banned: banned === undefined ? undefined : banned === 'yes',
              limit,
              offset,
            },
          },
        }),
      ),
  });

  const applySearch = () => {
    setQ(draftQ);
    setPage(1);
  };

  const columns: ColumnDef<SpaceRow, unknown>[] = [
    {
      accessorKey: 'name',
      header: t('col.space'),
      cell: ({ row }) => (
        <button
          className="text-sm font-medium hover:underline"
          onClick={() => navigate(`/spaces/${row.original.id}`)}
        >
          {row.original.name}
          <Badge variant="outline" className="ml-1.5 font-normal">
            {row.original.slug}
          </Badge>
        </button>
      ),
    },
    {
      accessorKey: 'space_type',
      header: t('col.type'),
      cell: ({ row }) =>
        row.original.space_type === 'personal' ? (
          <StatusBadge tone="blue">{t('type.personal')}</StatusBadge>
        ) : (
          <Badge variant="secondary">{t('type.shared')}</Badge>
        ),
    },
    {
      accessorKey: 'tenant_id',
      header: t('col.tenant'),
      cell: ({ row }) => row.original.tenant_id ?? t('platformLevel'),
    },
    { accessorKey: 'owner_name', header: 'Owner', cell: ({ row }) => row.original.owner_name ?? '-' },
    { accessorKey: 'member_count', header: t('col.memberCount') },
    { accessorKey: 'asset_count', header: t('col.assetCount') },
    {
      accessorKey: 'storage_bytes',
      header: t('col.storage'),
      cell: ({ row }) => formatBytes(row.original.storage_bytes),
    },
    {
      accessorKey: 'review_required',
      header: t('col.reviewRequired'),
      cell: ({ row }) =>
        row.original.review_required ? (
          <StatusBadge tone="amber">{t('toggle.on')}</StatusBadge>
        ) : (
          <Badge variant="outline">{t('toggle.off')}</Badge>
        ),
    },
    {
      accessorKey: 'banned',
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.banned ? (
          <StatusBadge tone="red">{t('status.banned')}</StatusBadge>
        ) : (
          <StatusBadge tone="green">{t('status.normal')}</StatusBadge>
        ),
    },
    {
      accessorKey: 'created_at',
      header: t('field.createdAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => (
        <Button variant="link" size="xs" className="px-0" onClick={() => navigate(`/spaces/${row.original.id}`)}>
          {t('viewDetail')}
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('title')} />
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-64"
          placeholder={t('searchPlaceholder')}
          value={draftQ}
          onChange={(e) => setDraftQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') applySearch();
          }}
        />
        <Button variant="outline" size="icon" aria-label={t('action.search')} onClick={applySearch}>
          <Search />
        </Button>
        <Select
          value={spaceType ?? 'all'}
          onValueChange={(v) => {
            setSpaceType(v === 'all' ? undefined : v);
            setPage(1);
          }}
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder={t('filter.typePlaceholder')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('action.all')}</SelectItem>
            <SelectItem value="shared">{t('type.sharedSpace')}</SelectItem>
            <SelectItem value="personal">{t('type.personalSpace')}</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={banned ?? 'all'}
          onValueChange={(v) => {
            setBanned(v === 'all' ? undefined : v);
            setPage(1);
          }}
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder={t('filter.banPlaceholder')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('action.all')}</SelectItem>
            <SelectItem value="yes">{t('status.banned')}</SelectItem>
            <SelectItem value="no">{t('status.normal')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        error={isError ? error : undefined}
        total={data?.total}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
      />
    </div>
  );
}
