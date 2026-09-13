import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Dot, MessageSquarePlus, Send, ShieldAlert, Waypoints } from 'lucide-react';
import { api } from '@loomvec/sdk-ts';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { Switch } from '@loomvec/ui/components/ui/switch';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@loomvec/ui/components/ui/collapsible';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { streamChatAnswer, type ChatCitation, type GraphEvidence } from '@/chat';

/**
 * P3-WEB-01 空间问答：会话列表 / SSE 流式渲染 / 引用 [n] 点击跳转定位 /
 * 图谱证据链面板（05 文档 §5.5）。
 * 约定：无 locator 的引用不渲染编号跳转；LLM 不可用时入口禁用并提示。
 */

interface SessionItem {
  session_id: string;
  space_id: string;
  title: string;
}

interface MessageItem {
  message_id: string;
  role: string;
  content: string;
  citations: unknown[];
  graph_evidence: unknown[];
}

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  citations?: ChatCitation[];
  graphEvidence?: GraphEvidence[];
  streaming?: boolean;
}

/** 把答案中的 [n] 拆成可点击引用标记。 */
function renderAnswer(
  text: string,
  citations: ChatCitation[] | undefined,
  onCite: (c: ChatCitation) => void,
) {
  const parts = text.split(/(\[\d{1,3}\])/g);
  return parts.map((part, i) => {
    const m = part.match(/^\[(\d{1,3})\]$/);
    if (!m || !citations) return <span key={i}>{part}</span>;
    const idx = Number(m[1]);
    const citation = citations.find((c) => c.index === idx);
    if (!citation) return <span key={i}>{part}</span>;
    return (
      <button
        key={i}
        type="button"
        className="mx-0.5 align-super text-xs font-semibold text-primary hover:underline"
        title={citation.text_snippet}
        onClick={(e) => {
          e.stopPropagation();
          onCite(citation);
        }}
      >
        [{idx}]
      </button>
    );
  });
}

function citationLabel(c: ChatCitation): string {
  const loc = c.locator ?? {};
  if (loc.pages?.length) return `第 ${loc.pages.map((p) => p + 1).join(',')} 页`;
  if (loc.time_start !== undefined)
    return `${Math.floor(loc.time_start / 60)}分${Math.round(loc.time_start % 60)}秒起`;
  if (loc.start_line !== undefined) return `行 ${loc.start_line}-${loc.end_line ?? ''}`;
  return '无定位';
}

function jumpToCitation(navigate: ReturnType<typeof useNavigate>, c: ChatCitation) {
  const loc = c.locator ?? {};
  const params = new URLSearchParams();
  if (loc.pages?.length) params.set('page', String(loc.pages[0] + 1));
  if (loc.time_start !== undefined) params.set('t', String(loc.time_start));
  const qs = params.toString();
  navigate(`/a/${c.asset_id}${qs ? `?${qs}` : ''}`);
}

export function ChatPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [useGraph, setUseGraph] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const status = useQuery({
    queryKey: ['chat-status', spaceId],
    queryFn: () =>
      api.GET('/api/v1/spaces/{space_id}/chat/status', { params: { path: { space_id: spaceId! } } }),
    enabled: !!spaceId,
  });

  const sessions = useQuery({
    queryKey: ['chat-sessions', spaceId],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/spaces/{space_id}/chat/sessions', {
        params: { path: { space_id: spaceId! } },
      });
      return resp.data as unknown as { items: SessionItem[]; total: number } | undefined;
    },
    enabled: !!spaceId,
  });

  const messages = useQuery({
    queryKey: ['chat-messages', spaceId, sessionId],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/spaces/{space_id}/chat/sessions/{session_id}/messages', {
        params: { path: { space_id: spaceId!, session_id: sessionId! } },
      });
      return resp.data as unknown as { items: MessageItem[]; total: number } | undefined;
    },
    enabled: !!spaceId && !!sessionId,
  });

  useEffect(() => {
    if (!sessionId && sessions.data?.items?.length) {
      setSessionId(sessions.data.items[0].session_id);
    }
  }, [sessions.data, sessionId]);

  useEffect(() => {
    if (messages.data?.items && sessionId) {
      setMsgs(
        messages.data.items.map((m) => ({
          role: m.role as 'user' | 'assistant',
          content: m.content,
          citations: m.citations as ChatCitation[],
          graphEvidence: m.graph_evidence as GraphEvidence[],
        })),
      );
    }
  }, [messages.data, sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [msgs]);

  const createSession = useMutation({
    mutationFn: () =>
      api.POST('/api/v1/spaces/{space_id}/chat/sessions', {
        params: { path: { space_id: spaceId! } },
        body: {},
      }),
    onSuccess: (resp) => {
      const s = resp.data as unknown as { session_id: string } | undefined;
      if (s) setSessionId(s.session_id);
      setMsgs([]);
      queryClient.invalidateQueries({ queryKey: ['chat-sessions', spaceId] });
    },
  });

  const ask = useMutation({
    mutationFn: async (question: string) => {
      let sid = sessionId;
      if (!sid) {
        const resp = await api.POST('/api/v1/spaces/{space_id}/chat/sessions', {
          params: { path: { space_id: spaceId! } },
          body: { title: question.slice(0, 50) },
        });
        const s = resp.data as { session_id: string } | undefined;
        sid = s?.session_id ?? null;
        if (sid) {
          setSessionId(sid);
          queryClient.invalidateQueries({ queryKey: ['chat-sessions', spaceId] });
        }
      }
      if (!sid) throw new Error('会话创建失败');

      setMsgs((prev) => [...prev, { role: 'user', content: question }]);
      setMsgs((prev) => [...prev, { role: 'assistant', content: '', streaming: true }]);

      await streamChatAnswer(spaceId!, sid, question, useGraph ? true : false, {
        onMeta: (meta) => {
          setMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.streaming) {
              next[next.length - 1] = {
                ...last,
                citations: meta.citations,
                graphEvidence: meta.graph_evidence,
              };
            }
            return next;
          });
        },
        onDelta: (text) => {
          setMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.streaming) next[next.length - 1] = { ...last, content: last.content + text };
            return next;
          });
        },
        onDone: () => {
          setMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.streaming) next[next.length - 1] = { ...last, streaming: false };
            return next;
          });
          queryClient.invalidateQueries({ queryKey: ['chat-messages', spaceId, sessionId ?? sid] });
        },
        onError: (message) => {
          setErrorMsg(message);
          setMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.streaming) next[next.length - 1] = { ...last, streaming: false };
            return next;
          });
        },
      });
    },
  });

  const submit = () => {
    const q = input.trim();
    if (!q || ask.isPending) return;
    setInput('');
    setErrorMsg(null);
    ask.mutate(q);
  };

  const chatDisabled = (status.data as unknown as { enabled?: boolean } | undefined)?.enabled === false;

  return (
    <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
      <Card className="h-fit gap-2 py-4">
        <CardHeader className="flex-row items-center justify-between border-b pb-2">
          <CardTitle className="text-sm">会话</CardTitle>
          <Button
            size="icon"
            variant="ghost"
            title="新建会话"
            onClick={() => createSession.mutate()}
          >
            <MessageSquarePlus className="size-4" />
          </Button>
        </CardHeader>
        <CardContent className="space-y-1">
          {(sessions.data?.items ?? []).map((s) => (
            <button
              key={s.session_id}
              type="button"
              className={`w-full truncate rounded px-2 py-1.5 text-left text-sm hover:bg-muted ${
                s.session_id === sessionId ? 'bg-muted font-medium' : ''
              }`}
              onClick={() => {
                setSessionId(s.session_id);
                setMsgs([]);
              }}
            >
              {s.title || '新会话'}
            </button>
          ))}
          {sessions.data && (sessions.data.items?.length ?? 0) === 0 && (
            <p className="px-2 py-1 text-sm text-muted-foreground">暂无会话</p>
          )}
        </CardContent>
      </Card>

      <Card className="flex min-h-[70vh] flex-col gap-0 py-4">
        <CardHeader className="flex-row items-center justify-between border-b pb-3">
          <CardTitle className="text-base">空间问答</CardTitle>
          <span className="flex items-center gap-2 text-sm text-muted-foreground">
            图谱召回
            <Switch checked={useGraph} onCheckedChange={setUseGraph} />
          </span>
        </CardHeader>
        <CardContent className="flex-1 space-y-4 overflow-y-auto pt-4">
          {chatDisabled && (
            <div className="flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              <ShieldAlert className="size-4" />
              问答暂不可用：{(status.data as unknown as { reason?: string } | undefined)?.reason ?? 'LLM 未配置'}（检索不受影响）
            </div>
          )}
          {msgs.length === 0 && !ask.isPending && (
            <EmptyState
              title="向知识空间提问"
              description="答案基于本空间检索来源生成，引用可点击回溯到原文位置。"
            />
          )}
          {msgs.map((m, i) => (
            <div key={i} className={m.role === 'user' ? 'flex justify-end' : ''}>
              <div
                className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
                  m.role === 'user' ? 'bg-primary text-primary-foreground' : 'bg-muted'
                }`}
              >
                {m.role === 'assistant'
                  ? renderAnswer(m.content || (m.streaming ? '……' : ''), m.citations, (c) =>
                      jumpToCitation(navigate, c),
                    )
                  : m.content}
                {m.streaming && m.content && (
                  <Dot className="inline size-4 animate-pulse text-primary" />
                )}
                {m.role === 'assistant' && (m.citations?.length ?? 0) > 0 && (
                  <div className="mt-2 border-t pt-2">
                    <p className="mb-1 text-xs font-medium text-muted-foreground">引用来源</p>
                    <ul className="space-y-1">
                      {m.citations!.map((c) => (
                        <li key={c.index} className="text-xs">
                          <span className="mr-1 font-semibold">[{c.index}]</span>
                          <button
                            type="button"
                            className="text-primary hover:underline"
                            onClick={() => jumpToCitation(navigate, c)}
                          >
                            {c.asset_name}
                          </button>
                          <span className="ml-1 text-muted-foreground">
                            {citationLabel(c)} · {c.text_snippet.slice(0, 60)}…
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {m.role === 'assistant' && (m.graphEvidence?.length ?? 0) > 0 && (
                  <Collapsible className="mt-2">
                    <CollapsibleTrigger className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                      <Waypoints className="size-3" />
                      图谱证据链（{m.graphEvidence!.length}）
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <ul className="mt-1 space-y-1 rounded bg-background/60 p-2">
                        {m.graphEvidence!.map((ev, j) => (
                          <li key={j} className="text-xs">
                            <span className="font-medium">{ev.head.name}</span>
                            <span className="mx-1 text-primary">—{ev.relation.type}→</span>
                            <span className="font-medium">{ev.tail.name}</span>
                            {ev.hops > 1 && <span className="ml-1 text-muted-foreground">(2跳)</span>}
                          </li>
                        ))}
                      </ul>
                    </CollapsibleContent>
                  </Collapsible>
                )}
              </div>
            </div>
          ))}
          {errorMsg && (
            <p className="text-sm text-destructive">
              {errorMsg}
              {errorMsg.includes('中断') && (
                <button
                  type="button"
                  className="ml-2 text-primary hover:underline"
                  onClick={() => {
                    const lastUser = [...msgs].reverse().find((m) => m.role === 'user');
                    if (lastUser) ask.mutate(lastUser.content);
                  }}
                >
                  重试
                </button>
              )}
            </p>
          )}
          <div ref={bottomRef} />
        </CardContent>
        <CardContent className="border-t pt-3">
          <div className="flex items-center gap-2">
            <Input
              placeholder={chatDisabled ? '问答暂不可用' : '输入问题，Enter 发送…'}
              value={input}
              disabled={chatDisabled}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.nativeEvent.isComposing) submit();
              }}
            />
            <Button size="icon" disabled={chatDisabled || ask.isPending || !input.trim()} onClick={submit}>
              {ask.isPending ? <Spinner className="size-4" /> : <Send className="size-4" />}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
