import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2, Volume2 } from 'lucide-react';
import { api, getStoredToken } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { formatClock } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

/**
 * P3-WEB-02/03 音视频定位器（05 文档 §5.6）：
 * - 播放器 seek 到 time_start、片段区间进度条高亮；
 * - 关键帧侧栏（点击 seek）；转写文本随播放同步高亮；
 * - 懒转码过渡：转码进行中以关键帧 + 进度条提示，完成后自动可播。
 *
 * 字幕接口需 Bearer 鉴权而 <track> 无法携带请求头：先带 token 取回 WebVTT 文本，
 * 再以 Blob URL 挂给 <track>（解析结果同时驱动转写高亮面板）。
 */

interface Keyframe {
  time_start: number;
  time_end: number | null;
  url: string | null;
}

interface PlaybackData {
  mode: 'native' | 'transcoding' | 'audio' | 'unsupported';
  mime_type: string;
  url: string | null;
  progress: number | null;
  duration: number | null;
  keyframes: Keyframe[];
}

interface Cue {
  start: number;
  end: number;
  text: string;
}

/** 极简 WebVTT 解析（字幕轨与转写高亮共用；后端固定输出 HH:MM:SS.mmm 时间轴）。 */
export function parseWebVTT(text: string): Cue[] {
  const cues: Cue[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})/);
    if (!m) continue;
    const toSec = (_h: string, _m: string, _s: string, _ms: string) =>
      Number(_h) * 3600 + Number(_m) * 60 + Number(_s) + Number(_ms) / 1000;
    const start = toSec(m[1], m[2], m[3], m[4]);
    const end = toSec(m[5], m[6], m[7], m[8]);
    const body: string[] = [];
    for (let j = i + 1; j < lines.length && lines[j].trim() !== ''; j++) body.push(lines[j]);
    if (body.length) cues.push({ start, end, text: body.join(' ') });
  }
  return cues;
}

export function MediaPlayer({ assetId, seekTo }: { assetId: string; seekTo?: number }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const { t } = useTranslation('assetDetail');
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState<number | null>(null);
  const [subtitles, setSubtitles] = useState<{ text: string; blobUrl: string } | null>(null);

  // 懒转码进行中轮询；其余模式一次性获取
  const playback = useQuery({
    queryKey: ['playback', assetId],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/assets/{asset_id}/playback', {
        params: { path: { asset_id: assetId } },
      });
      return (resp.data ?? null) as PlaybackData | null;
    },
    refetchInterval: (q) =>
      q.state.data?.mode === 'transcoding' ? 2000 : false,
  });

  const data = playback.data ?? null;
  const mode = data?.mode;

  // 字幕（转写）：带鉴权取回文本 → Blob URL 供 <track>，解析结果驱动高亮面板
  useEffect(() => {
    if (!mode || mode === 'transcoding' || mode === 'unsupported') return;
    const token = getStoredToken();
    let cancelled = false;
    let blobUrl: string | null = null;
    fetch(`/api/v1/assets/${assetId}/subtitles`, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    })
      .then(async (r) => {
        // 404 = 无转写；401/5xx 同样按无字幕处理，不打断播放
        if (!r.ok) return;
        const text = await r.text();
        blobUrl = URL.createObjectURL(new Blob([text], { type: 'text/vtt' }));
        if (!cancelled) setSubtitles({ text, blobUrl });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [assetId, mode]);

  // 卸载时释放 Blob URL
  useEffect(() => {
    return () => {
      if (subtitles) URL.revokeObjectURL(subtitles.blobUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const cues = useMemo(
    () => (subtitles ? parseWebVTT(subtitles.text) : []),
    [subtitles],
  );
  const activeCue = cues.find((c) => currentTime >= c.start && currentTime <= c.end);

  // ?t= 或检索命中 seek：元数据未加载前设置 currentTime 会被浏览器丢弃，等 loadedmetadata 再执行
  useEffect(() => {
    if (seekTo === undefined || !data || mode === 'transcoding') return;
    const el = videoRef.current ?? audioRef.current;
    if (!el) return;
    const apply = () => {
      el.currentTime = seekTo;
      el.play().catch(() => undefined);
    };
    if (el.readyState >= 1) {
      apply();
      return;
    }
    el.addEventListener('loadedmetadata', apply, { once: true });
    return () => el.removeEventListener('loadedmetadata', apply);
  }, [seekTo, data, mode]);

  const doSeek = (t: number) => {
    const el = videoRef.current ?? audioRef.current;
    if (el) {
      el.currentTime = t;
      setCurrentTime(t);
      el.play().catch(() => undefined);
    }
  };

  if (playback.isLoading) {
    return (
      <div className="grid place-items-center py-10">
        <Spinner className="size-5 text-muted-foreground" />
      </div>
    );
  }
  if (!data || mode === 'unsupported') {
    return <p className="text-sm text-muted-foreground">{t('media.unsupported')}</p>;
  }

  const dur = duration ?? data.duration ?? 0;

  if (mode === 'transcoding') {
    // 懒转码过渡：关键帧 + 进度提示（05 文档 §5.6）
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <Loader2 className="mr-1 inline size-4 animate-spin" />
          {t('media.transcoding', { percent: Math.round((data.progress ?? 0) * 100) })}
        </div>
        <Progress value={(data.progress ?? 0) * 100} />
        {data.keyframes.length > 0 && (
          <KeyframeRail keyframes={data.keyframes} currentTime={currentTime} onSeek={doSeek} />
        )}
      </div>
    );
  }

  const isAudio = mode === 'audio';

  return (
    <div className="space-y-3">
      {isAudio ? (
        <div className="flex items-center gap-3 rounded-lg border p-4">
          <Volume2 className="size-5 text-muted-foreground" />
          <audio
            ref={audioRef}
            src={data.url ?? undefined}
            controls
            className="w-full"
            onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
            onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
          >
            {subtitles && (
              <track
                kind="subtitles"
                srcLang="zh"
                label={t('media.trackLabel')}
                src={subtitles.blobUrl}
              />
            )}
          </audio>
        </div>
      ) : (
        <video
          ref={videoRef}
          src={data.url ?? undefined}
          controls
          className="max-h-[60vh] w-full rounded-lg bg-black"
          onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        >
          {subtitles && (
            <track
              kind="subtitles"
              srcLang="zh"
              label={t('media.trackLabel')}
              src={subtitles.blobUrl}
            />
          )}
        </video>
      )}

      {/* 片段区间高亮进度条 */}
      {dur > 0 && (
        <div className="space-y-1">
          <input
            type="range"
            min={0}
            max={dur}
            step={0.5}
            value={currentTime}
            onChange={(e) => doSeek(Number(e.target.value))}
            className="w-full"
          />
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>{formatClock(currentTime)}</span>
            <span>{formatClock(dur)}</span>
          </div>
          {data.keyframes.length > 0 && (
            <div className="relative h-1.5 rounded bg-muted">
              {data.keyframes.map((k, i) => {
                const left = dur > 0 ? (k.time_start / dur) * 100 : 0;
                return (
                  <button
                    key={i}
                    type="button"
                    title={t('media.keyframeAt', { time: formatClock(k.time_start) })}
                    className="absolute top-0 h-1.5 w-2 rounded bg-primary/60 hover:bg-primary"
                    style={{ left: `${left}%` }}
                    onClick={() => doSeek(k.time_start)}
                  />
                );
              })}
            </div>
          )}
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-[1fr_200px]">
        {/* 转写同步高亮 */}
        {cues.length > 0 && (
          <div className="max-h-48 space-y-1 overflow-y-auto rounded border p-2">
            {cues.map((c, i) => (
              <button
                key={i}
                type="button"
                className={`block w-full rounded px-2 py-1 text-left text-xs hover:bg-muted ${
                  activeCue === c ? 'bg-primary/10 font-medium text-primary' : ''
                }`}
                onClick={() => doSeek(c.start)}
              >
                <span className="mr-1 text-muted-foreground">{formatClock(c.start)}</span>
                {c.text}
              </button>
            ))}
          </div>
        )}
        {data.keyframes.length > 0 && (
          <KeyframeRail keyframes={data.keyframes} currentTime={currentTime} onSeek={doSeek} />
        )}
      </div>
    </div>
  );
}

function KeyframeRail({
  keyframes,
  currentTime,
  onSeek,
}: {
  keyframes: Keyframe[];
  currentTime: number;
  onSeek: (t: number) => void;
}) {
  const { t } = useTranslation('assetDetail');
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{t('media.keyframesTitle')}</p>
      <div className="flex gap-1 overflow-x-auto">
        {keyframes.map((k, i) => (
          <Button
            key={i}
            variant="ghost"
            size="sm"
            className={`h-auto shrink-0 flex-col gap-0.5 p-1 ${
              currentTime >= k.time_start &&
              currentTime < (k.time_end ?? Number.MAX_SAFE_INTEGER)
                ? 'ring-2 ring-primary'
                : ''
            }`}
            onClick={() => onSeek(k.time_start)}
          >
            {k.url ? (
              <img src={k.url} alt={t('media.sceneIndex', { index: i + 1 })} className="h-12 w-20 rounded object-cover" />
            ) : (
              <div className="grid h-12 w-20 place-items-center rounded bg-muted text-xs">
                {t('media.sceneIndex', { index: i + 1 })}
              </div>
            )}
            <span className="text-[10px] text-muted-foreground">{formatClock(k.time_start)}</span>
          </Button>
        ))}
      </div>
    </div>
  );
}
