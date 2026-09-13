import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
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
  const [q, setQ] = useState('');
  const [draftQ, setDraftQ] = useState('');
  const [spaceType, setSpaceType] = useState<string | undefined>();
  const [banned, setBanned] = useState<string | undefined>();
  const [page, setPage] = useState(1);

  const limit = PAGE_SIZE;
  const offset = (page - 1) * PAGE_SIZE;

  const { data, isFetching } = useQuery({
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
      header: '空间',
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
      header: '类型',
      cell: ({ row }) =>
        row.original.space_type === 'personal' ? (
          <StatusBadge tone="blue">个人</StatusBadge>
        ) : (
          <Badge variant="secondary">共享</Badge>
        ),
    },
    {
      accessorKey: 'tenant_id',
      header: '所属租户',
      cell: ({ row }) => row.original.tenant_id ?? '—（平台级）',
    },
    { accessorKey: 'owner_name', header: 'Owner', cell: ({ row }) => row.original.owner_name ?? '-' },
    { accessorKey: 'member_count', header: '成员数' },
    { accessorKey: 'asset_count', header: '资产数' },
    {
      accessorKey: 'storage_bytes',
      header: '存储',
      cell: ({ row }) => formatBytes(row.original.storage_bytes),
    },
    {
      accessorKey: 'review_required',
      header: '先审后见',
      cell: ({ row }) =>
        row.original.review_required ? (
          <StatusBadge tone="amber">开启</StatusBadge>
        ) : (
          <Badge variant="outline">关闭</Badge>
        ),
    },
    {
      accessorKey: 'banned',
      header: '状态',
      cell: ({ row }) =>
        row.original.banned ? (
          <StatusBadge tone="red">已封禁</StatusBadge>
        ) : (
          <StatusBadge tone="green">正常</StatusBadge>
        ),
    },
    {
      accessorKey: 'created_at',
      header: '创建时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <Button variant="link" size="xs" className="px-0" onClick={() => navigate(`/spaces/${row.original.id}`)}>
          详情
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="空间治理" />
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-64"
          placeholder="按名称/slug 搜索"
          value={draftQ}
          onChange={(e) => setDraftQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') applySearch();
          }}
        />
        <Button variant="outline" size="icon" aria-label="搜索" onClick={applySearch}>
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
            <SelectValue placeholder="空间类型" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            <SelectItem value="shared">共享空间</SelectItem>
            <SelectItem value="personal">个人空间</SelectItem>
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
            <SelectValue placeholder="封禁状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            <SelectItem value="yes">已封禁</SelectItem>
            <SelectItem value="no">正常</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        total={data?.total}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
      />
    </div>
  );
}
