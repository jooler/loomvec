/**
 * 问答 SSE 客户端：POST 提问 + 流式解析（EventSource 不支持 POST）。
 * 协议：event: meta / delta / done / error，data 为 JSON（见 api/services/qa.py）。
 * 端点为用户级 /api/v1/chat/*；图谱联合召回由后端常态开启，无请求级开关。
 */
import { getStoredToken } from '@loomvec/sdk-ts';

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

export interface GraphEvidence {
  head: { key: string; name: string; type: string };
  relation: { type: string; chunk_ids?: string[] };
  tail: { key: string; name: string; type: string };
  hops: number;
}

export interface StreamHandlers {
  onMeta?: (meta: { citations: ChatCitation[]; graph_evidence: GraphEvidence[] }) => void;
  onDelta?: (text: string) => void;
  onDone?: (data: {
    message_id: string;
    citations: ChatCitation[];
    graph_evidence: GraphEvidence[];
    answer: string;
  }) => void;
  onError?: (message: string) => void;
}

export async function streamChatAnswer(
  sessionId: string,
  question: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = getStoredToken();
  const resp = await fetch(`/api/v1/chat/sessions/${sessionId}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-Id': crypto.randomUUID(),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let detail = `请求失败（${resp.status}）`;
    try {
      const err = await resp.json();
      if (err?.message) detail = err.message;
    } catch {
      /* 保留默认文案 */
    }
    handlers.onError?.(detail);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    // SSE 帧以空行分隔
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      handleFrame(frame, handlers);
    }
  }
  buffer += decoder.decode(); // flush 多字节字符的尾部残片
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
  interface Frame {
    citations?: ChatCitation[];
    graph_evidence?: GraphEvidence[];
    text?: string;
    message?: string;
    message_id?: string;
    answer?: string;
  }
  let data: Frame;
  try {
    data = JSON.parse(dataLines.join('\n')) as Frame;
  } catch {
    return;
  }
  if (event === 'meta') {
    handlers.onMeta?.({
      citations: data.citations ?? [],
      graph_evidence: data.graph_evidence ?? [],
    });
  } else if (event === 'delta') {
    handlers.onDelta?.(data.text ?? '');
  } else if (event === 'done') {
    handlers.onDone?.({
      message_id: data.message_id ?? '',
      citations: data.citations ?? [],
      graph_evidence: data.graph_evidence ?? [],
      answer: data.answer ?? '',
    });
  } else if (event === 'error') {
    handlers.onError?.(data.message ?? '生成失败');
  }
}
