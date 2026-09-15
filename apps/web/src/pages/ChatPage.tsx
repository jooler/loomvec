import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Dot, Send, ShieldAlert, Waypoints } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@loomvec/ui/components/ui/collapsible';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { MultiSelect } from '@/components/multi-select';
import { useChatSessions, useMySpaces, usePublicSpaces } from '@/hooks';
import { extractApiError } from '@/utils';
import { streamChatAnswer, type ChatCitation, type GraphEvidence } from '@/chat';

/**
 * 问答（用户级，05 文档 §5.5）：全高三段式布局，会话列表常驻 AppLayout 侧栏上部
 * （本页不再内嵌侧栏）。召回空间范围按会话配置（空 = 我的全部空间），可随时调整；
 * 图谱联合召回常态开启（无开关）；引用 [n] 点击回溯定位；LLM 不可用时入口禁用。
 */

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
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [input, setInput] = useState('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // 本会话召回范围的多选本地值；null = 未改动，回显服务端值
  const [scopeOverride, setScopeOverride] = useState<string[] | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // 流式进行中标记：阻止消息列表查询回灌清掉正在生成的气泡
  // （新会话首问时 sessionId 刚建立，messages 查询会以空列表先返回）
  const streamingRef = useRef(false);

  const spaces = useMySpaces();
  // 已链接的公共空间也是合法检索源（P5）；与成员空间一起出现在召回范围选择器中
  const publicSpaces = usePublicSpaces();

  const status = useQuery({
    queryKey: ['chat-status'],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/chat/status', {});
      return resp.data as unknown as { enabled?: boolean; reason?: string } | undefined;
    },
  });

  const sessions = useChatSessions();

  const messages = useQuery({
    queryKey: ['chat-messages', sessionId],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/chat/sessions/{session_id}/messages', {
        params: { path: { session_id: sessionId! } },
      });
      return resp.data as unknown as { items: MessageItem[]; total: number } | undefined;
    },
    enabled: !!sessionId,
  });

  // 切换会话（路由变化）：清空气泡与本地范围改动；流式中（首问自动建会话跳转）不清
  useEffect(() => {
    if (!streamingRef.current) {
      setMsgs([]);
      setErrorMsg(null);
      setScopeOverride(null);
    }
  }, [sessionId]);

  useEffect(() => {
    if (messages.data?.items && sessionId && !streamingRef.current) {
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

  const updateScope = useMutation({
    mutationFn: async (scopeIds: string[]) => {
      const { error } = await api.PATCH('/api/v1/chat/sessions/{session_id}', {
        params: { path: { session_id: sessionId! } },
        body: { scope_space_ids: scopeIds },
      });
      if (error) throw new Error(extractApiError(error, '更新召回范围失败'));
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['chat-sessions'] }),
    onError: (e) => {
      // 失败回滚为服务端值，避免本地显示与实际召回范围不一致
      setScopeOverride(null);
      toast.error(e.message);
    },
  });

  const ask = useMutation({
    mutationFn: async (question: string) => {
      let sid = sessionId;
      if (!sid) {
        const resp = await api.POST('/api/v1/chat/sessions', {
          body: { title: question.slice(0, 50) },
        });
        const s = resp.data as unknown as { session_id?: string } | undefined;
        sid = s?.session_id;
        if (sid) {
          navigate(`/chat/${sid}`);
          void queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
        }
      }
      if (!sid) throw new Error('会话创建失败');

      setMsgs((prev) => [...prev, { role: 'user', content: question }]);
      setMsgs((prev) => [...prev, { role: 'assistant', content: '', streaming: true }]);
      streamingRef.current = true;

      try {
        await streamChatAnswer(sid, question, {
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
            void queryClient.invalidateQueries({ queryKey: ['chat-messages', sid] });
            void queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
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
      } finally {
        streamingRef.current = false;
      }
    },
    // 网络层异常（fetch/reader 中断）兜底：结束气泡并提示，否则流式标记永不落地
    onError: (e) => {
      setErrorMsg(e instanceof Error ? e.message : '生成失败，请重试');
      setMsgs((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.streaming) next[next.length - 1] = { ...last, streaming: false };
        return next;
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

  const chatDisabled = status.data?.enabled === false;
  const currentSession = (sessions.data?.items ?? []).find((s) => s.session_id === sessionId);
  const scopeIds = scopeOverride ?? currentSession?.scope_space_ids ?? [];
  const spaceOptions = [
    ...(spaces.data ?? []).map((s) => ({ value: s.id, label: s.name })),
    ...(publicSpaces.data ?? [])
      .filter((s) => s.linked)
      .map((s) => ({ value: s.id, label: `${s.name}（公共）` })),
  ];

  return (
    <div className="flex h-svh min-w-0 flex-col">
      {/* 顶栏：会话标题 + 召回范围（会话列表在 AppLayout 侧栏上部） */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-background px-4 py-3">
        <h1 className="truncate text-base font-semibold">{currentSession?.title || '对话'}</h1>
        {sessionId && (
          <div className="flex items-center gap-2">
            <span className="shrink-0 text-sm text-muted-foreground">召回空间</span>
            <MultiSelect
              value={scopeIds}
              options={spaceOptions}
              placeholder="全部空间（我的全部可检索空间）"
              loading={spaces.isLoading || publicSpaces.isLoading || sessions.isLoading}
              onChange={(v) => {
                setScopeOverride(v);
                updateScope.mutate(v);
              }}
            />
            <StatusBadge tone="purple">图谱联合召回</StatusBadge>
          </div>
        )}
      </div>

      {/* 消息区：滚动仅限此区域 */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-4">
          {chatDisabled && (
            <div className="flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              <ShieldAlert className="size-4" />
              问答暂不可用：{status.data?.reason ?? 'LLM 未配置'}（检索不受影响）
            </div>
          )}
          {msgs.length === 0 && !ask.isPending && (
            <EmptyState
              title={sessionId ? '向所选空间提问' : '开始一段对话'}
              description="答案基于会话召回范围内的检索来源生成，引用可点击回溯到原文位置。"
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
                            {ev.hops > 1 && <span className="ml-1 text-muted-foreground">（{ev.hops} 跳）</span>}
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
            </p>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* 输入区 */}
      <div className="border-t bg-background px-4 py-3">
        <div className="mx-auto flex w-full max-w-3xl items-center gap-2">
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
      </div>
    </div>
  );
}
