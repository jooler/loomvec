import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Separator } from '@loomvec/ui/components/ui/separator';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import type { AssetDetailData } from './use-asset-queries';

/**
 * 图片体验卡（P2）：renditions 缩略图网格 / 原图查看（Dialog 预览）/ EXIF 信息面板。
 */
export function ImagePanel(props: { asset: AssetDetailData; mainImageUrl: string | null }) {
  const { asset: a } = props;
  const { t } = useTranslation('assetDetail');
  const [viewing, setViewing] = useState<string | null>(null);

  const exif = (a.version_meta?.parse as Record<string, unknown> | undefined)?.exif as
    | Record<string, unknown>
    | undefined;
  const caption = (a.asset_meta as Record<string, unknown> | undefined)?.caption;
  const thumbnails = a.renditions
    .filter((r) => r.kind === 'thumbnail' && r.url)
    .map((r) => ({ url: r.url as string, width: r.width, height: r.height }));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t('image.title')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-3">
          {props.mainImageUrl && (
            <button
              type="button"
              className="overflow-hidden rounded-md border"
              onClick={() => setViewing(props.mainImageUrl)}
            >
              <img src={props.mainImageUrl} alt={a.name} className="w-80 object-cover" />
            </button>
          )}
          {thumbnails.map((r) => (
            <button
              key={r.url}
              type="button"
              className="overflow-hidden rounded-md border"
              onClick={() => setViewing(r.url)}
            >
              <img
                src={r.url}
                alt={`thumbnail ${r.width ?? ''}x${r.height ?? ''}`}
                className="w-40 object-cover"
              />
            </button>
          ))}
          {!props.mainImageUrl && thumbnails.length === 0 && (
            <p className="text-sm text-muted-foreground">{t('image.noImages')}</p>
          )}
        </div>
        {caption ? (
          <p className="text-sm text-muted-foreground">
            {t('image.captionLabel')}
            {String(caption)}
          </p>
        ) : null}
        {exif && Object.keys(exif).length > 0 && (
          <>
            <div className="flex items-center gap-3 pt-2">
              <span className="text-sm text-muted-foreground">{t('image.exifTitle')}</span>
              <Separator className="flex-1" />
            </div>
            <DescriptionList cols={3}>
              {Object.entries(exif).map(([k, v]) => (
                <DescriptionItem key={k} label={k}>
                  {String(v)}
                </DescriptionItem>
              ))}
            </DescriptionList>
          </>
        )}
        {thumbnails.some((r) => r.width || r.height) && (
          <p className="text-sm text-muted-foreground">
            {t('image.thumbSizesLabel')}
            {thumbnails.map((r) => `${r.width ?? '?'}×${r.height ?? '?'}`).join('、')}
          </p>
        )}
      </CardContent>

      {/* 原图查看（替代 antd Image.PreviewGroup 预览） */}
      <Dialog open={viewing !== null} onOpenChange={(o) => !o && setViewing(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{a.name}</DialogTitle>
          </DialogHeader>
          {viewing && <img src={viewing} alt={a.name} className="max-h-[75vh] w-full object-contain" />}
        </DialogContent>
      </Dialog>
    </Card>
  );
}
