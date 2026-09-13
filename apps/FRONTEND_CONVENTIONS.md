# 前端编码规范（apps/admin 与 apps/web 通用）

本文是两个前端的统一编码规范（2026-09 起）。两个 app 组件/页面代码同构，除路由器（web 用 BrowserRouter，admin 用 HashRouter）外无差异。

## 1. 技术栈与导入

- React 18 + TS strict + Vite 6 + react-router 7 + @tanstack/react-query 5（main.tsx 已配好，重试 1 次 / staleTime 30s）。
- **共享组件包 `@loomvec/ui`**（`packages/ui`，2026-09-13 起单源）：shadcn/ui 基础组件（`@loomvec/ui/components/ui/*`）、跨 app 业务组件（ConfirmAction/DataTable/DescriptionList/EmptyState/PageHeader/StatCard/StatusBadge，`@loomvec/ui/components/*`）、共享工具（`@loomvec/ui/lib/format`：extractApiError/formatBytes/formatDateTime/formatQuota/formatSeconds/sha256Hex）与 use-mobile hook。包内为 TS 源码直出（无构建产物），各 app 的 vite/vitest/tsconfig 已配好 `@loomvec/ui/*` 解析；**新增共享组件改 `packages/ui`，禁止再往两个 app 各拷一份**。
- app 本地组件仍在 `@/components/*`（web：ReviewStatusTag/media-player/multi-select；admin：ReasonModal/SecretModal）。各 app 的 `utils.ts` 是再导出层 + 应用专属业务字典（web：配额/审核/角色文案）。
- Tailwind v4 类扫描：两 app 的 `index.css` 已 `@source` 指向 `packages/ui/src`，新组件无需额外配置。
- **禁止引入 antd / @ant-design/***；**禁止内联 `style={{}}`**（一律 Tailwind 类）；**禁止新增 CSS 文件**。
- `cn` 从 `cn` 包导入：`import { cn } from 'cn'`。
- 路径别名 `@/` → `src/`（`@/auth`、`@/api`、`@/utils`、`@/components/...`）。
- API：`import { api, unwrap } from '@/api'`（admin）/ `import { api } from '@loomvec/sdk-ts'`（web，见 hooks）。openapi-fetch 用法不变。
- react-query 查询 key 与缓存语义保持稳定，改动需评估所有消费方。

## 2. 页面骨架

```tsx
import { PageHeader } from '@/components/page-header';

export function XxxPage() {
  return (
    <div className="space-y-4">
      <PageHeader
        title="页面标题"
        description="可选的一句话说明"
        actions={<Button>主操作</Button>}   // 可选
      />
      {/* 内容 */}
    </div>
  );
}
```
- 页面用命名导出，导出名与 App.tsx 引用一致。
- 旧 `Card title=...` 等价写法：`<Card><CardHeader><CardTitle>..</CardTitle></CardHeader><CardContent>..</CardContent></Card>`。
- 布局用 Tailwind flex/grid；`<div className="flex items-center gap-2">` 是操作按钮组的标准写法。

## 3. 反馈与确认

- 提示反馈一律 sonner：`toast.success/error/warning/info(...)`（`import { toast } from 'sonner'`）。
- mutation/query catch 里：`toast.error(e instanceof Error ? e.message : '操作失败')`。
- 危险/重要操作确认 → `ConfirmAction`（`@/components/confirm-action`）：trigger 传按钮元素。
- 危险操作二次确认 + 必填理由（审计留痕）→ `ReasonModal`（`@/components/ReasonModal`）。
- 一次性密钥展示 → `SecretModal`（同上文件）。
- 页面内 Spin 等待 → `<div className="grid place-items-center py-10"><Spinner className="size-5 text-muted-foreground" /></div>`。
- 空态/结果占位 → `EmptyState`（`@/components/empty-state`）；403 类由 App.tsx 守卫处理，页面不用管。

## 4. 表格

用 `DataTable`（`@/components/data-table`，@tanstack/react-table 封装，带骨架加载/空态/分页）：

```tsx
import type { ColumnDef } from '@tanstack/react-table';
import { DataTable } from '@/components/data-table';

const columns: ColumnDef<TenantRow, unknown>[] = [
  { accessorKey: 'name', header: '名称', cell: ({ row }) => (
      <button className="text-sm font-medium hover:underline" onClick={() => navigate(`/tenants/${row.original.id}`)}>{row.original.name}</button>
    ) },
  { id: 'actions', header: '操作', cell: ({ row }) => <div className="flex gap-1">...</div> },
];

<DataTable columns={columns} data={data?.items} loading={isFetching}
  total={data?.total} page={page} pageSize={PAGE_SIZE} onPageChange={setPage} />
```

- 服务端分页：`const [page, setPage] = useState(1)`；offset/limit 按 API 契约计算。
- 小表（详情页内的嵌套表）不传分页 props（≤10 行不出分页条）。
- 链接列：优先 `<Link>`（react-router）；纯跳转用 `<button className="hover:underline">` 或 `variant="link"` Button。
- 空态文案通过 `emptyTitle/emptyDescription` 定制。

## 5. 状态与徽标

- 彩色状态 → `StatusBadge tone="green|red|amber|blue|purple|gray"`（`@/components/status-badge`）：green=成功/正常、red=失败/危险、amber=待处理/警告、blue=进行中/信息、purple=特殊、gray=中性。
- 无色标签 → `<Badge variant="outline">` 或 `<Badge variant="secondary">`。
- web 端审核状态用现成的 `ReviewStatusTag`。

## 6. 表单（react-hook-form + zod）

```tsx
const schema = z.object({
  name: z.string().min(1, '请输入名称'),
  quota: z.number().min(0),                     // 数字字段
  enabled: z.boolean(),                          // 开关字段
  plan: z.string(),                              // 下拉字段
});
const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: {...} });

<Input {...form.register('name')} />
<Input type="number" {...form.register('quota', { valueAsNumber: true })} />
<Switch checked={form.watch('enabled')} onCheckedChange={(v) => form.setValue('enabled', v)} />
<Select value={form.watch('plan')} onValueChange={(v) => form.setValue('plan', v)}>...</Select>
<form onSubmit={form.handleSubmit(onSubmit)}>...</form>
```

- 校验文案写入 zod schema（如 `min(1, '请输入…')`）。
- 提交中禁用：`disabled={form.formState.isSubmitting}`。
- 弹窗内表单：Dialog 关闭时 `form.reset()`。
- 数字输入：`<Input type="number" {...form.register('x', { valueAsNumber: true })}` + schema `z.number()`。
- 日期筛选 → `<Input type="date" />`（受控 string，空串视为不过滤）。

## 7. 弹窗与抽屉

- 对话框 → `Dialog`（`@/components/ui/dialog`）；侧滑面板 → `Sheet`。
- 关闭时 reset 表单/状态；提交中 footer 按钮 `disabled` + 文案「保存中…」。
- 自定义 footer → `<DialogFooter>` 内放 Button（取消用 `variant="outline"`）。

## 8. 详情页键值对与统计

- 键值对列表 → `DescriptionList/DescriptionItem`（`@/components/description-list`）。
- 统计卡片 → `StatCard`（`@/components/stat-card`）；进度条 → ui `Progress value={0-100}`。

## 9. 其他约定

- 标题/正文/次要文本：语义标签 + Tailwind（`text-lg font-semibold` / `text-sm` / `text-muted-foreground`）；行内代码 `<code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">`。
- 可复制文本 → code + Copy 按钮（参考 `ReasonModal.tsx` 的 SecretModal 实现）。
- 下拉菜单 → `DropdownMenu`；页签/分段 → `Tabs`；气泡提示 → ui `Tooltip`；提示条 → ui `Alert`。
- 文件拖拽上传：手写拖拽区（`onDragOver` preventDefault + `onDrop`），内嵌 `<input type="file" className="hidden">` + Button 触发，进度用 ui `Progress`；上传逻辑在 `@/upload.ts`（web）。
- 图标一律 `lucide-react`。
- **React 18 注意**：shadcn registry 新版组件默认面向 React 19；本项目仍在 React 18，`Input`/`Textarea` 必须保留 `forwardRef`（RHF register 依赖 ref 取值），升级 React 19 前不得移除。

## 10. 业务规则

- 权限闸（canWrite/isSuperAdmin）禁用逻辑、危险操作文案、reason 必填、审计提示语不得随意改动。
- 查询 key、invalidate 范围、分页大小（PAGE_SIZE）、默认值、错误兜底文案是行为契约，改动需同步评估。
- UI 文案为中文；新增共享组件放 `packages/ui/src/components/<name>.tsx`（app 专属放 `src/components/`），先确认无同名/同职责组件。
