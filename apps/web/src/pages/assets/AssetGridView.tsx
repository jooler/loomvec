import { FileText } from 'lucide-react';
import { Card, CardContent } from '@loomvec/ui/components/ui/card';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { ReviewStatusTag } from '@/components/review-status-tag';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { formatBytes } from '@/utils';
import { STATUS_TONE } from './constants';
import type { AssetListItem } from './use-assets-list';

/** 缩略图网格视图：图片资产取详情缩略图，其余以文件图标占位。 */
export function AssetGridView(props: {
  items: AssetListItem[];
  thumbById: Map<string, string | null>;
  onOpen: (assetId: string) => void;
}) {
  return (
    <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
      {props.items.map((a) => {
        const thumb = props.thumbById.get(a.id);
        const open = () => props.onOpen(a.id);
        return (
          <Card key={a.id} className="gap-2 py-3 transition-shadow hover:shadow-md">
            {a.is_image && thumb ? (
              <img
                alt={a.name}
                src={thumb}
                className="h-[120px] w-full cursor-pointer object-cover"
                onClick={open}
              />
            ) : (
              <div
                className="grid h-[120px] cursor-pointer place-items-center bg-muted/60"
                onClick={open}
              >
                {a.is_image ? (
                  <Spinner className="size-4 text-muted-foreground" />
                ) : (
                  <FileText className="size-10 text-muted-foreground/70" />
                )}
              </div>
            )}
            <CardContent className="space-y-1.5 px-3">
              <button
                className="block max-w-full truncate text-left text-[13px] font-medium hover:underline"
                onClick={open}
              >
                {a.name}
              </button>
              <div className="flex flex-wrap items-center gap-1">
                <StatusBadge tone={STATUS_TONE[a.status]?.tone}>
                  {STATUS_TONE[a.status]?.text ?? a.status}
                </StatusBadge>
                {a.review_status && <ReviewStatusTag status={a.review_status} />}
                <span className="text-xs text-muted-foreground">{formatBytes(a.size_bytes)}</span>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
