/** 页头：替代 antd PageContainer 的标题区（标题 + 描述 + 右侧操作区）。 */
export function PageHeader(props: {
  title: React.ReactNode;
  description?: React.ReactNode;
  /** 右侧操作按钮区 */
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={props.className ?? 'mb-6 flex flex-wrap items-start justify-between gap-3'}>
      <div className="min-w-0 space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{props.title}</h1>
        {props.description && (
          <p className="text-sm text-muted-foreground">{props.description}</p>
        )}
      </div>
      {props.actions && <div className="flex flex-wrap items-center gap-2">{props.actions}</div>}
    </div>
  );
}
