import { describe, expect, it } from 'vitest';
import type { SettingItem } from '@/types';
import {
  buildChanges,
  draftFromItems,
  isLocalBase,
  type AiChannelDraft,
} from './aiProviders';

const PORTS = { vllm: 38010, infinity: 38011 };

const items: SettingItem[] = [
  { key: 'ai.llm.base_url', group: 'ai', description: '', value: 'http://127.0.0.1:38010/v1' },
  { key: 'ai.llm.model', group: 'ai', description: '', value: 'qwen2.5-1.5b' },
  { key: 'ai.llm.api_key', group: 'ai', description: '', value: null, sensitive: true },
  {
    key: 'ai.embedding.base_url',
    group: 'ai',
    description: '',
    value: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  },
  { key: 'ai.embedding.model', group: 'ai', description: '', value: 'text-embedding-v4' },
  { key: 'ai.clip.base_url', group: 'ai', description: '', value: 'http://127.0.0.1:38011' },
  { key: 'ai.clip.model', group: 'ai', description: '', value: 'jina-clip-v2' },
  { key: 'ai.clip.api_style', group: 'ai', description: '', value: 'infinity' },
] as SettingItem[];

const UP_SUGGESTIONS = {
  llm: { available: true, base_url: 'http://127.0.0.1:38010/v1', model: 'qwen2.5-1.5b', api_key: 'local', api_style: 'openai' },
  embedding: { available: true, base_url: 'http://127.0.0.1:38011', model: 'bge-m3', api_key: 'local' },
  rerank: { available: true, base_url: 'http://127.0.0.1:38011', model: 'bge-reranker-v2-m3', api_key: 'local', api_style: 'openai' },
  clip: { available: false, base_url: 'http://127.0.0.1:38011', model: 'jina-clip-v2', api_key: 'local', api_style: 'infinity' },
  vlm: { available: false },
};

describe('isLocalBase', () => {
  it('matches loopback hosts on the given port only', () => {
    expect(isLocalBase('http://127.0.0.1:38010/v1', 38010)).toBe(true);
    expect(isLocalBase('http://localhost:38011/', 38011)).toBe(true);
    expect(isLocalBase('http://127.0.0.1:38010/v1', 38011)).toBe(false);
    expect(isLocalBase('https://api.deepseek.com', 38010)).toBe(false);
    expect(isLocalBase(null, 38010)).toBe(false);
    expect(isLocalBase('', 38010)).toBe(false);
  });
});

describe('draftFromItems', () => {
  it('derives location from base_url port and never reveals api_key', () => {
    const d = draftFromItems(items, PORTS);
    expect(d.llm.location).toBe('local');
    expect(d.llm.model).toBe('qwen2.5-1.5b');
    expect(d.llm.apiKey).toBe('');
    expect(d.embedding.location).toBe('cloud');
    expect(d.embedding.model).toBe('text-embedding-v4');
    expect(d.clip.location).toBe('local');
    expect(d.clip.apiStyle).toBe('infinity');
    // 无 api_style 键的通道兜底 openai
    expect(d.rerank.apiStyle).toBe('openai');
  });
});

describe('buildChanges', () => {
  const init = draftFromItems(items, PORTS);
  const clone = (d: typeof init) => JSON.parse(JSON.stringify(d)) as typeof init;

  it('emits nothing when nothing changed', () => {
    expect(buildChanges(clone(init), init, undefined)).toEqual([]);
  });

  it('cloud channel diffs only touched keys and keeps blank api_key as keep-current', () => {
    const d = clone(init);
    d.embedding.model = 'text-embedding-v3';
    d.embedding.apiKey = 'sk-new';
    d.embedding.baseUrl = init.embedding.baseUrl; // 未变
    const changes = buildChanges(d, init, UP_SUGGESTIONS);
    expect(changes).toContainEqual({ key: 'ai.embedding.model', value: 'text-embedding-v3' });
    expect(changes).toContainEqual({ key: 'ai.embedding.api_key', value: 'sk-new' });
    expect(changes.filter((c) => c.key === 'ai.embedding.base_url')).toEqual([]);
  });

  it('local channel overwrites the full standard bundle from suggestions', () => {
    const d = clone(init);
    d.embedding.location = 'local';
    d.embedding.model = 'bge-m3'; // 表单切换本地时会同步建议模型名
    const changes = buildChanges(d, init, UP_SUGGESTIONS);
    expect(changes).toContainEqual({ key: 'ai.embedding.base_url', value: 'http://127.0.0.1:38011' });
    expect(changes).toContainEqual({ key: 'ai.embedding.api_key', value: 'local' });
    expect(changes).toContainEqual({ key: 'ai.embedding.model', value: 'bge-m3' });
    // embedding 在注册表无 api_style 键 → 不写
    expect(changes.filter((c) => c.key === 'ai.embedding.api_style')).toEqual([]);
  });

  it('rerank local also pins api_style', () => {
    const d = clone(init);
    d.rerank.location = 'local';
    d.rerank.model = 'bge-reranker-v2-m3';
    const changes = buildChanges(d, init, UP_SUGGESTIONS);
    expect(changes).toContainEqual({ key: 'ai.rerank.api_style', value: 'openai' });
    expect(changes).toContainEqual({ key: 'ai.rerank.model', value: 'bge-reranker-v2-m3' });
  });

  it('falls back to draft values when switching local without suggestions', () => {
    const d = clone(init);
    d.embedding.location = 'local'; // 云端 → 本地，无探测数据可用
    const changes = buildChanges(d, init, undefined);
    expect(changes).toContainEqual({ key: 'ai.embedding.base_url', value: d.embedding.baseUrl });
    expect(changes).toContainEqual({ key: 'ai.embedding.api_key', value: 'local' });
    expect(changes).toContainEqual({ key: 'ai.embedding.model', value: d.embedding.model });
  });

  it('cloud switch with cleared local endpoint emits base_url + model + api_key', () => {
    const d = clone(init);
    d.llm.location = 'cloud';
    d.llm.baseUrl = ''; // 表单切云端时清空本地端点
    d.llm.model = 'deepseek-chat';
    d.llm.apiKey = 'sk-cloud';
    const changes = buildChanges(d, init, UP_SUGGESTIONS);
    expect(changes).toContainEqual({ key: 'ai.llm.base_url', value: '' });
    expect(changes).toContainEqual({ key: 'ai.llm.model', value: 'deepseek-chat' });
    expect(changes).toContainEqual({ key: 'ai.llm.api_key', value: 'sk-cloud' });
  });

  it('already-local channel with no diff emits nothing', () => {
    const d = clone(init); // llm 初始即本地
    const changes = buildChanges(d, init, UP_SUGGESTIONS);
    expect(changes.filter((c) => c.key.startsWith('ai.llm.'))).toEqual([]);
  });

  it('accepts a full AiChannelDraft record shape', () => {
    const draft: Record<string, AiChannelDraft> = JSON.parse(JSON.stringify(init));
    draft.embedding.baseUrl = 'https://other.example.com/v1';
    const changes = buildChanges(draft as typeof init, init, undefined);
    expect(changes).toContainEqual({
      key: 'ai.embedding.base_url',
      value: 'https://other.example.com/v1',
    });
  });
});
