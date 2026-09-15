# 前端编码规范（apps/web · apps/admin · apps/ops 通用）

本文是三个前端的统一编码规范（2026-09 起）。三个 app 组件/页面代码同构，除路由器（web 用 BrowserRouter，admin/ops 用 HashRouter）外无差异。运营端（apps/ops，P5）定位与信息架构见 docs/12。

## 1. 技术栈与导入

- React 18 + TS strict + Vite 6 + react-router 7 + @tanstack/react-query 5（main.tsx 已配好，重试 1 次 / staleTime 30s）。
- **共享组件包 `@loomvec/ui`**（`packages/ui`，2026-09-13 起单源）：shadcn/ui 基础组件（`@loomvec/ui/components/ui/*`）、跨 app 业务组件（ConfirmAction/DataTable/DescriptionList/EmptyState/PageHeader/StatCard/StatusBadge，`@loomvec/ui/components/*`）、共享工具（`@loomvec/ui/lib/format`：extractApiError/formatBytes/formatDateTime/formatQuota/formatSeconds/sha256Hex）与 use-mobile hook。包内为 TS 源码直出（无构建产物），各 app 的 vite/vitest/tsconfig 已配好 `@loomvec/ui/*` 解析；**新增共享组件改 `packages/ui`，禁止再往各 app 拷贝**。跨 app 共享的业务模块也在 ui 包：`@loomvec/ui/lib/upload`（三步直传管线，P5 自 web 提升）、`@loomvec/ui/components/review-status-tag`（审核徽标，P5 自 web 提升）。
- app 本地组件仍在 `@/components/*`（web：media-player/multi-select；admin：ReasonModal/SecretModal）。各 app 的 `utils.ts` 是再导出层 + 应用专属业务字典（web：配额/审核/角色文案）。
- Tailwind v4 类扫描：两 app 的 `index.css` 已 `@source` 指向 `packages/ui/src`，新组件无需额外配置。
- **禁止引入 antd / @ant-design/***；**禁止内联 `style={{}}`**（一律 Tailwind 类）；**禁止新增 CSS 文件**。
- `cn` 从 `cn` 包导入：`import { cn } from 'cn'`。
- 路径别名 `@/` → `src/`（`@/auth`、`@/api`、`@/utils`、`@/components/...`）。
- API：`import { api, unwrap } from '@/api'`（admin/ops）/ `import { api } from '@loomvec/sdk-ts'`（web，见 hooks）。openapi-fetch 用法不变。
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
- 审核状态用现成的 `ReviewStatusTag`（`@loomvec/ui/components/review-status-tag`，web/ops 共用）。

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
- 文件拖拽上传：手写拖拽区（`onDragOver` preventDefault + `onDrop`），内嵌 `<input type="file" className="hidden">` + Button 触发，进度用 ui `Progress`；上传逻辑用 `uploadFile`（`@loomvec/ui/lib/upload`；web 的 `@/upload.ts` 为再导出兼容层）。
- 图标一律 `lucide-react`。
- **React 18 注意**：shadcn registry 新版组件默认面向 React 19；本项目仍在 React 18，`Input`/`Textarea` 必须保留 `forwardRef`（RHF register 依赖 ref 取值），升级 React 19 前不得移除。

## 10. 业务规则

- 权限闸（canWrite/isSuperAdmin）禁用逻辑、危险操作文案、reason 必填、审计提示语不得随意改动。
- 查询 key、invalidate 范围、分页大小（PAGE_SIZE）、默认值、错误兜底文案是行为契约，改动需同步评估。
- UI 文案一律经 i18n（当前仅 zh-CN，见 §11），源码不得硬编码用户可见文案；新增共享组件放 `packages/ui/src/components/<name>.tsx`（app 专属放 `src/components/`），先确认无同名/同职责组件。

## 11. 国际化（i18n）

基础设施：`i18next` + `react-i18next`，共享核心在 `@loomvec/ui/i18n`（`initI18n` / `t` / `setLocale` / `SUPPORTED_LOCALES`）。当前仅实现简体中文（`zh-CN`），结构与类型按多语言组织；功能稳定后新增语言 = 在 `packages/ui/src/i18n/locales/<code>/` 与各 app 的 `locales/<code>/` 补齐同名命名空间 JSON，并在 `packages/ui/src/i18n/locales.ts` 注册。

### 11.1 资源与命名空间

- 语言包只存在于 `locales/zh-CN/<ns>.json`；key 用英文 camelCase、按功能分组（两层为宜，如 `member.add`）；value 为中文文案（逐字保留，含全角标点）。
- 共享命名空间（`packages/ui/src/i18n/locales/zh-CN/`）：
  - `common`：跨 app 通用词汇（action/field/feedback/state/pagination 五组，如 `action.save`=保存、`feedback.operationFailed`=操作失败）；
  - `ui`：共享组件与工具文案（`finder.*`、`chunks.*`、`viewer.*`、`assetStatus.*`、`reviewStatus.*`、`upload.*`、`format.*` 等）。
- app 命名空间（`apps/<app>/src/i18n/locales/zh-CN/`）按功能域拆分：web = auth/layout/spaces/assets/assetDetail/chat/graph/review/notifications/profile/search；admin = auth/layout/overview/tenants/users/spaces/models/reviews/pipeline/audit/open/settings/system/components；ops = auth/layout/overview/groups/spaces。增删命名空间需同步 app 的 `src/i18n/index.ts` 与 `i18next.d.ts`。
- **复用优先**：common/ui 已有的词汇不得在 app 语言包重复定义；app 内跨命名空间确需共享的词条，就近期望各存一份（fallback 链只覆盖 common → ui）。

### 11.2 取值写法

- React 组件/hook：`const { t } = useTranslation('<ns>')`，一律**裸 key**（不带 `ns:` 前缀）；命名空间回退链 = 自身 ns → common → ui（运行时 `fallbackNS` 与类型均已配置）。
- 非 React 模块（模块级 zod schema、`menu.ts`/`constants.ts` 字典等纯 .ts）：`import { t } from '@/i18n'`，key **必须带 `'ns:'` 前缀**（如 `t('spaces:nameRequired')`）。main.tsx / test-setup.ts 已保证 import 阶段完成初始化。
- 插值用 `{{var}}`（如 `"memberCount": "{{count}} 名成员"` → `t('memberCount', { count })`）；插值变量名由 tsc 校验，必须与 JSON 模板一致。
- 动态 key（状态字典）必须带 `defaultValue` 才能通过类型检查：`t(\`assetStatus.${s}\`, { defaultValue: s })`；「字典带文案」的结构（`Record<string, {tone, text}>`）只留 tone/color，文案在渲染处按动态 key 取。
- 回调/Effect 依赖数组用到 `t` 时把 `t` 加入 deps（引用稳定）；组件内勿用 `t` 作局部变量名（遮蔽翻译函数）。
- 类型增强：各 app `src/i18n/i18next.d.ts`（`CustomTypeOptions`）提供 key 自动补全、存在性与插值校验；lint 即可拦截坏 key。

### 11.3 已知边界（未来多语言时再处理）

- 模块级取值（zod 消息、菜单名）在 import 阶段求值，运行时切换语言不会重算——支持多语言时需改为工厂函数或在组件内求值。
- 纯函数库（`@loomvec/ui/lib/format`、`lib/upload`）经默认实例取 `t`，语言切换后下一次调用即生效，但已渲染的字符串不重算。
