/**
 * AI 供方分层表单的纯逻辑：本地端点判定、草稿初始化、变更键计算。
 * 与后端契约（services/api .../services/local_models.py）保持一致：
 * 本地判定 = host 为 127.0.0.1/localhost 且端口匹配 vLLM/Infinity。
 */
import type { SettingItem } from '@/types';

export const AI_CHANNELS = ['llm', 'embedding', 'rerank', 'vlm', 'clip'] as const;
export type AiChannel = (typeof AI_CHANNELS)[number];

/** rerank / clip 额外有 api_style 键（与后端注册表一致）。 */
export const CHANNELS_WITH_API_STYLE: readonly AiChannel[] = ['rerank', 'clip'];

/** 各通道依赖的本地推理服务（vlm 未本地化，无本地档）。 */
export const CHANNEL_SERVICE: Record<AiChannel, 'vllm' | 'infinity' | null> = {
  llm: 'vllm',
  embedding: 'infinity',
  rerank: 'infinity',
  vlm: null,
  clip: 'infinity',
};

/** isLocalBase 判断 base_url 是否指向本机 port（忽略路径；host 限回环地址）。 */
export function isLocalBase(url: string | null | undefined, port: number): boolean {
  if (!url) return false;
  const rest = url.split('://')[1];
  if (!rest) return false;
  const hostPart = rest.replace(/\/+$/, '').split('/')[0];
  const [host, portPart] = hostPart.split(':');
  if (host !== '127.0.0.1' && host !== 'localhost') return false;
  if (!portPart) return port === 80 || port === 443;
  const p = Number(portPart);
  return Number.isInteger(p) && p === port;
}

export interface AiChannelDraft {
  location: 'local' | 'cloud';
  baseUrl: string;
  /** 云端密钥输入；空 = 保留已存值（sensitive 不回显）。 */
  apiKey: string;
  model: string;
  apiStyle: string;
}

type ItemMap = Record<string, SettingItem | undefined>;

const itemValue = (items: ItemMap, key: string): string =>
  typeof items[key]?.value === 'string' ? (items[key]?.value as string) : '';

/** 从 settings 键值 + 本地探测结果初始化各通道草稿（api_key 永不回显，置空）。 */
export function draftFromItems(
  items: SettingItem[],
  ports: { vllm: number; infinity: number },
): Record<AiChannel, AiChannelDraft> {
  const map: ItemMap = Object.fromEntries(items.map((s) => [s.key, s]));
  const draft = {} as Record<AiChannel, AiChannelDraft>;
  for (const ch of AI_CHANNELS) {
    const baseUrl = itemValue(map, `ai.${ch}.base_url`);
    const service = CHANNEL_SERVICE[ch];
    const port = service === 'vllm' ? ports.vllm : ports.infinity;
    const isLocal = service !== null && isLocalBase(baseUrl, port);
    draft[ch] = {
      location: isLocal ? 'local' : 'cloud',
      baseUrl,
      apiKey: '',
      model: itemValue(map, `ai.${ch}.model`),
      apiStyle: itemValue(map, `ai.${ch}.api_style`) || (ch === 'clip' ? 'dashscope' : 'openai'),
    };
  }
  return draft;
}

export interface SettingChange {
  key: string;
  value: string;
}

/** local-runtime 探测返回的每通道建议配置（available=false 时前端禁选本地）。 */
export interface LocalSuggestion {
  available: boolean;
  base_url?: string;
  model?: string;
  api_key?: string;
  api_style?: string;
}

/** 本地档 api_style 兜底（探测数据缺失时）：与 scripts/start-models-gpu.sh 一致。 */
const LOCAL_API_STYLE: Partial<Record<AiChannel, string>> = { rerank: 'openai', clip: 'infinity' };

/** 计算需要写入的键值（相对 items 当前值）。location=local：从云端切本地时
 * 全量写标准配置（含 api_key=local），已在本地的只写有差异的键（本地 api_key
 * 恒 local 且敏感不回显，无差异不重写；api_style 仅探测建议与现值不同才写）。 */
export function buildChanges(
  draft: Record<AiChannel, AiChannelDraft>,
  initial: Record<AiChannel, AiChannelDraft>,
  suggestions: Record<string, LocalSuggestion> | undefined,
): SettingChange[] {
  const changes: SettingChange[] = [];
  for (const ch of AI_CHANNELS) {
    const d = draft[ch];
    const init = initial[ch];
    const s = suggestions?.[ch];
    const keys = {
      baseUrl: `ai.${ch}.base_url`,
      apiKey: `ai.${ch}.api_key`,
      model: `ai.${ch}.model`,
      apiStyle: `ai.${ch}.api_style`,
    };
    if (d.location === 'local') {
      const targetBaseUrl = s?.base_url ?? d.baseUrl;
      const targetModel = d.model || s?.model || '';
      const targetStyle = s?.api_style ?? LOCAL_API_STYLE[ch] ?? 'openai';
      if (init.location !== 'local') {
        // 云端 → 本地：整体覆盖为标准配置
        changes.push({ key: keys.baseUrl, value: targetBaseUrl });
        changes.push({ key: keys.apiKey, value: s?.api_key ?? 'local' });
        changes.push({ key: keys.model, value: targetModel });
        if (CHANNELS_WITH_API_STYLE.includes(ch)) {
          changes.push({ key: keys.apiStyle, value: targetStyle });
        }
      } else {
        if (targetBaseUrl !== init.baseUrl) changes.push({ key: keys.baseUrl, value: targetBaseUrl });
        if (targetModel !== init.model) changes.push({ key: keys.model, value: targetModel });
        if (
          CHANNELS_WITH_API_STYLE.includes(ch) &&
          s?.api_style != null &&
          s.api_style !== init.apiStyle
        ) {
          changes.push({ key: keys.apiStyle, value: s.api_style });
        }
      }
      continue;
    }
    if (d.baseUrl !== init.baseUrl) changes.push({ key: keys.baseUrl, value: d.baseUrl });
    if (d.model !== init.model) changes.push({ key: keys.model, value: d.model });
    if (d.apiKey !== '') changes.push({ key: keys.apiKey, value: d.apiKey });
    if (
      CHANNELS_WITH_API_STYLE.includes(ch) &&
      d.apiStyle !== init.apiStyle
    ) {
      changes.push({ key: keys.apiStyle, value: d.apiStyle });
    }
  }
  return changes;
}
