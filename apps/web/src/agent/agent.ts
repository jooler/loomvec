/**
 * 智能体对话 SSE 客户端（P5，14 文档 §5.3）：POST 提问 + ReadableStream 手解帧。
 * 对话由 dsh 全量接管（无 legacy 分支）。
 *
 * 事件：meta / delta / reasoning / tool_start / tool_end / citations / usage /
 * status / done / error。适配层职责（与 UI 解耦的契约边界）：
 * - 引用编号归一：后端 ref_items 用 `n`，前端 ChatCitation 渲染用 `index`，
 *   在本层完成映射 → ChatPage 的引用渲染与 jumpToCitation 零改动；
 * - 会话形状归一：agent 会话 `id` → 侧栏 `session_id`。
 */
import { getStoredToken } from '@loomvec/sdk-ts';
import { api } from '@loomvec/sdk-ts';
import { t } from '@/i18n';

/** 引用条目（[n] 编号对应答案标注，locator 非空可跳转定位）。 */
export interface ChatCitation {
  index: number;
  unit_id: string;
  asset_id: string;
  asset_name: string;
  unit_type: string;
  title: string | null;
  locator: { pages?: number[]; time_start?: number; time_end?: number; start_line?: number; end_line?: number };
  text_snippet: string;
  locatable: boolean;
}

/** 图谱证据链（实体→关系→实体路径）。 */
export interface GraphEvidence {
  head: { key: string; name: string; type: string };
  relation: { type: string; chunk_ids?: string[] };
  tail: { key: string; name: string; type: string };
  hops: number;
}

/** 后端 ref_items 条目（n 为引用编号）。 */
interface RawRefItem {
  n: number;
  unit_id: string;
  asset_id: string;
  asset_name: string;
  unit_type: string;
  title: string | null;
  text_snippet: string;
  locator: ChatCitation['locator'];
}

export interface AgentUsage {
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  uncached_input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  steps?: number;
  cache_hit_rate?: number;
  ttft_ms?: number;
  turns?: number;
}

export interface AgentToolCall {
  call_id: string;
  tool: string;
  args?: string;
  ok?: boolean;
  result_summary?: string;
}

export interface AgentMessage {
  role: 'user' | 'assistant';
  content: string;
  reasoning?: string;
  citations?: ChatCitation[];
  graphEvidence?: GraphEvidence[];
  toolCalls?: AgentToolCall[];
  usage?: AgentUsage;
  streaming?: boolean;
}

export interface StreamHandlers {
  onMeta?: (meta: { session_id: string; message_id: string; model: string }) => void;
  onDelta?: (text: string) => void;
  onReasoning?: (text: string) => void;
  onToolStart?: (call: AgentToolCall) => void;
  onToolEnd?: (call: AgentToolCall) => void;
  onCitations?: (citations: ChatCitation[], evidence: GraphEvidence[]) => void;
  onUsage?: (usage: AgentUsage) => void;
  onStatus?: (status: 'running' | 'idle' | 'cancelled') => void;
  onDone?: (data: {
    message_id: string;
    answer: string;
    citations: ChatCitation[];
    graph_evidence: GraphEvidence[];
    usage: AgentUsage;
  }) => void;
  onError?: (message: string, code?: string) => void;
}

function normalizeCitation(item: RawRefItem): ChatCitation {
  return {
    index: item.n,
    unit_id: item.unit_id,
    asset_id: item.asset_id,
    asset_name: item.asset_name,
    unit_type: item.unit_type,
    title: item.title,
    locator: item.locator ?? {},
    text_snippet: item.text_snippet,
    locatable: !!item.locator && Object.keys(item.locator).length > 0,
  };
}

export async function streamAgentAnswer(
  sessionId: string,
  question: string,
  handlers: StreamHandlers,
  attachmentIds?: string[],
  signal?: AbortSignal,
): Promise<void> {
  const token = getStoredToken();
  const resp = await fetch(`/api/v1/agent/sessions/${sessionId}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-Id': crypto.randomUUID(),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ question, attachment_ids: attachmentIds }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let detail = t('agent:requestFailedStatus', { status: resp.status });
    try {
      const err = await resp.json();
      if (err?.message) detail = err.message;
    } catch {
      /* 保留默认文案 */
    }
    handlers.onError?.(detail);
    return;
  }
  // 终止帧（done/error）缺失 = 流被上游中途掐断；补一个 error，避免 UI 永远停在"思考中"
  let terminated = false;
  await pumpSse(resp.body, {
    ...handlers,
    onDone: (d) => {
      terminated = true;
      handlers.onDone?.(d);
    },
    onError: (m, code) => {
      terminated = true;
      handlers.onError?.(m, code);
    },
    onStatus: (st) => {
      if (st === 'cancelled') terminated = true;
      handlers.onStatus?.(st);
    },
  });
  if (!terminated && !signal?.aborted) handlers.onError?.(t('agent:streamInterrupted'));
}

async function pumpSse(body: ReadableStream<Uint8Array>, handlers: StreamHandlers) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      handleFrame(frame, handlers);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) handleFrame(buffer, handlers);
}

function handleFrame(frame: string, handlers: StreamHandlers) {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return;
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(dataLines.join('\n')) as Record<string, unknown>;
  } catch {
    return;
  }
  switch (event) {
    case 'meta':
      handlers.onMeta?.({
        session_id: String(data.session_id ?? ''),
        message_id: String(data.message_id ?? ''),
        model: String(data.model ?? ''),
      });
      break;
    case 'delta':
      handlers.onDelta?.(String(data.text ?? ''));
      break;
    case 'reasoning':
      handlers.onReasoning?.(String(data.text ?? ''));
      break;
    case 'tool_start':
      handlers.onToolStart?.({
        call_id: String(data.call_id ?? ''),
        tool: String(data.tool ?? ''),
        args: data.args_partial ? String(data.args_partial) : String(data.args ?? ''),
      });
      break;
    case 'tool_end':
      handlers.onToolEnd?.({
        call_id: String(data.call_id ?? ''),
        tool: String(data.tool ?? ''),
        ok: data.ok !== false,
        result_summary: String(data.result_summary ?? ''),
      });
      break;
    case 'citations': {
      const raw = (data.citations ?? []) as RawRefItem[];
      handlers.onCitations?.(raw.map(normalizeCitation), (data.graph_evidence ?? []) as GraphEvidence[]);
      break;
    }
    case 'usage':
      handlers.onUsage?.(data as AgentUsage);
      break;
    case 'status':
      handlers.onStatus?.(data.status as 'running' | 'idle' | 'cancelled');
      break;
    case 'done':
      handlers.onDone?.({
        message_id: String(data.message_id ?? ''),
        answer: String(data.answer ?? ''),
        citations: ((data.citations ?? []) as RawRefItem[]).map(normalizeCitation),
        graph_evidence: (data.graph_evidence ?? []) as GraphEvidence[],
        usage: (data.usage ?? {}) as AgentUsage,
      });
      break;
    case 'error':
      handlers.onError?.(String(data.message ?? t('agent:generateFailed')), String(data.code ?? ''));
      break;
  }
}

// ---------------------------------------------------------------------------
// REST 辅助（会话 CRUD / 状态 / 取消 / 附件；类型来自 sdk-ts 生成契约）
// ---------------------------------------------------------------------------

export async function fetchAgentStatus(): Promise<{
  enabled: boolean;
  runtime_ok: boolean;
  reason: string | null;
}> {
  const { data, error } = await api.GET('/api/v1/agent/status', {});
  if (error) throw new Error('agent status unavailable');
  return data as unknown as { enabled: boolean; runtime_ok: boolean; reason: string | null };
}

export async function cancelAgentTurn(sessionId: string): Promise<void> {
  await api.POST('/api/v1/agent/sessions/{session_id}/cancel', {
    params: { path: { session_id: sessionId } },
  });
}

export async function uploadAgentAttachment(
  sessionId: string,
  file: File,
): Promise<{ attachment_id: string; path: string; size: number; mime: string }> {
  const token = getStoredToken();
  const form = new FormData();
  form.append('file', file);
  const resp = await fetch(`/api/v1/agent/sessions/${sessionId}/attachments`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  });
  if (!resp.ok) {
    let detail = t('agent:uploadFailed');
    try {
      const err = await resp.json();
      if (err?.message) detail = err.message;
    } catch {
      /* 保留默认文案 */
    }
    throw new Error(detail);
  }
  return (await resp.json()) as { attachment_id: string; path: string; size: number; mime: string };
}
