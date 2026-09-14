import {
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  useReactTable,
  type ColumnDef,
} from '@tanstack/react-table';
import { ChevronLeft, ChevronRight, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { Button } from './ui/button';
import { Skeleton } from './ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './ui/table';
import { EmptyState } from './empty-state';

/**
 * 通用数据表（@tanstack/react-table + shadcn Table）。
 * - 服务端分页：传 total/page/pageSize/onPageChange（react-query 按 limit/offset 查询的页面用这个）。
 * - 客户端分页：不传分页 props，数据超过 pageSize 时自动出分页条。
 * - 查询失败：传 error，展示错误提示（避免失败被空态伪装成“无数据”）。
 */
export function DataTable<TData>(props: {
  columns: ColumnDef<TData, unknown>[];
  data?: TData[];
  /** 覆盖行 key（默认尝试 row.id） */
  getRowId?: (row: TData, index: number) => string;
  loading?: boolean;
  /** 查询失败信息（优先于空态展示） */
  error?: Error | null;
  total?: number;
  page?: number;
  pageSize?: number;
  onPageChange?: (page: number) => void;
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  const { columns, data, loading } = props;
  const serverPaged =
    typeof props.total === 'number' &&
    typeof props.page === 'number' &&
    typeof props.pageSize === 'number' &&
    typeof props.onPageChange === 'function';
  const [internalPage, setInternalPage] = useState(0);

  const table = useReactTable({
    data: data ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    manualPagination: serverPaged,
    pageCount: serverPaged
      ? Math.max(1, Math.ceil((props.total ?? 0) / (props.pageSize ?? 1)))
      : undefined,
    ...(serverPaged
      ? {}
      : {
          state: { pagination: { pageIndex: internalPage, pageSize: 10 } },
          onPaginationChange: (updater: unknown) => {
            const next =
              typeof updater === 'function'
                ? (updater as (old: { pageIndex: number; pageSize: number }) => {
                    pageIndex: number;
                    pageSize: number;
                  })({ pageIndex: internalPage, pageSize: 10 })
                : (updater as { pageIndex: number; pageSize: number });
            setInternalPage(next.pageIndex);
          },
        }),
    getRowId: props.getRowId
      ? (row, index) => props.getRowId!(row, index)
      : (row) => String((row as { id?: unknown })?.id ?? ''),
  });

  const rows = table.getRowModel().rows;
  const colCount = columns.length;
  const totalPages = serverPaged
    ? Math.max(1, Math.ceil((props.total ?? 0) / (props.pageSize ?? 1)))
    : table.getPageCount();
  const current = serverPaged ? (props.page ?? 1) : table.getState().pagination.pageIndex + 1;
  const totalCount = serverPaged ? (props.total ?? 0) : table.getCoreRowModel().rows.length;
  const showPager = serverPaged ? true : table.getPageCount() > 1;

  return (
    <div className="space-y-3">
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id} className="bg-muted/50 hover:bg-muted/50">
                {hg.headers.map((header) => (
                  <TableHead key={header.id}>
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {loading ? (
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={`sk-${i}`}>
                  {Array.from({ length: colCount }).map((_, j) => (
                    <TableCell key={`sk-${i}-${j}`}>
                      <Skeleton className="h-4 w-full max-w-32" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : props.error ? (
              <TableRow>
                <TableCell colSpan={colCount} className="p-0">
                  <EmptyState
                    icon={TriangleAlert}
                    title="加载失败"
                    description={props.error.message || '请求出错，请稍后重试'}
                    className="[&>div:first-child]:bg-destructive/10 [&_svg]:text-destructive"
                  />
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={colCount} className="p-0">
                  <EmptyState title={props.emptyTitle} description={props.emptyDescription} />
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row) => (
                <TableRow key={row.id}>{row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}</TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      {showPager && !loading && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>共 {totalCount} 条</span>
          <div className="flex items-center gap-2">
            <span>
              第 {current} / {totalPages} 页
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={current <= 1}
              onClick={() =>
                serverPaged
                  ? props.onPageChange!(current - 1)
                  : setInternalPage((p) => Math.max(0, p - 1))
              }
            >
              <ChevronLeft /> 上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={current >= totalPages}
              onClick={() =>
                serverPaged
                  ? props.onPageChange!(current + 1)
                  : setInternalPage((p) => p + 1)
              }
            >
              下一页 <ChevronRight />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
