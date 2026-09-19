import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Brain, ChevronDown, FileUp, FolderOpen, Paperclip, Send, ShieldAlert, Square, Wrench, X } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@loomvec/ui/components/ui/collapsible';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { MultiSelect } from '@/components/multi-select';
import { useAgentSessions, useAgentStatus, useMySpaces, usePublicSpaces } from '@/hooks';
import { extractApiError } from '@/utils';
import {
  cancelAgentTurn,
  streamAgentAnswer,
  uploadAgentAttachment,
  type AgentMessage,
  type AgentToolCall,
  type ChatCitation,
  type GraphEvidence,
} from '@/agent/agent';
import { AnswerMarkdown, citationLabel, jumpToCitation } from '@/pages/chat-shared';

/**
 * 对话页（P5，14 文档 §9）：dsh 全量接管的智能体对话（无 legacy 分支）。
 * 五件套：流式文本 / 思维链折叠 / 工具调用卡 / 附件（文件+图片）/ 会话切换；
 * 引用 [n] 与 locator 跳转（chat-shared）；/agent/status 不可用时入口降级提示。
 * dsh 协议只有完成态 assistant/message 通知（无逐 token 推送）；网关按其内嵌
 * stream 记录的 chunk 原文与时间差，重放为逐块 delta/reasoning 帧（打字机效果）。
 */

interface HistoryItem {
  role: string;
  content: string;
  citations?: { n: number }[];
  graph_evidence?: unknown[];
  tool_calls?: { call_id: string; tool: string; ok?: boolean; result_summary?: string }[];
  created_at?: string | null;
}

/** 单条消息附件上限（对齐服务端 agent.session.attachments.max_per_message）。 */
const MAX_ATTACHMENTS = 5;

/** 待发送附件（上传完成后持有 attachment_id）。 */
interface PendingAttachment {
  attachment_id: string;
  name: string;
  is_image: boolean;
  size: number;
  preview_url?: string;
}

export function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const { t } = useTranslation('agent');
  const [input, setInput] = useState('');
  const [msgs, setMsgs] = useState<AgentMessage[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [scopeOverride, setScopeOverride] = useState<string[] | null>(null);
  // 草稿对话（P5.6）：/chat?project=<目录> 仅展示对话 UI；项目行内「新建对话」
  // 都落到同一 URL，重复点击不产生新草稿。首条消息发出时才创建真实会话。
  const draftProject = sessionId ? '' : (searchParams.get('project') ?? '');
  const [draftScope, setDraftScope] = useState<string[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const streamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  const spaces = useMySpaces();
  const publicSpaces = usePublicSpaces();
  const sessions = useAgentSessions();
  const agentStatus = useAgentStatus();
  const agentDisabled = agentStatus.data?.enabled === false;

  const messages = useQuery({
    queryKey: ['agent-messages', sessionId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/agent/sessions/{session_id}/messages', {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw new Error(extractApiError(error, t('loadHistoryFailed')));
      return data as unknown as { items: HistoryItem[]; total: number } | undefined;
    },
    enabled: !!sessionId,
  });

  useEffect(() => {
    if (!streamingRef.current) {
      setMsgs([]);
      setErrorMsg(null);
      setScopeOverride(null);
      setAttachments([]);
    }
  }, [sessionId]);

  // 切换草稿项目（或离开草稿）：重置草稿本地状态
  useEffect(() => {
    if (!streamingRef.current) {
      setInput('');
      setDraftScope([]);
    }
  }, [draftProject]);

  // 历史回灌（引用 n → index 归一在视图内完成，渲染层零特殊分支）
  useEffect(() => {
    if (messages.data?.items && sessionId && !streamingRef.current) {
      setMsgs(
        messages.data.items.map(
          (m: HistoryItem): AgentMessage => ({
            role: m.role as 'user' | 'assistant',
            content: m.content,
            citations: (m.citations ?? []).map(
              (c): ChatCitation => ({
                ...(c as unknown as ChatCitation),
                index: (c as unknown as { n: number }).n,
              }),
            ),
            graphEvidence: m.graph_evidence as GraphEvidence[],
            toolCalls: (m.tool_calls ?? []).map((tc) => ({ ...tc, ok: tc.ok !== false })),
          }),
        ),
      );
    }
  }, [messages.data, sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [msgs]);

  const patchSession = useMutation({
    mutationFn: async (body: { title?: string; scope_space_ids?: string[] }) => {
      const { error } = await api.PATCH('/api/v1/agent/sessions/{session_id}', {
        params: { path: { session_id: sessionId! } },
        body,
      });
      if (error) throw new Error(extractApiError(error, t('updateSessionFailed')));
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] }),
    onError: (e) => toast.error(e.message),
  });

  const onFiles = async (files: FileList | null) => {
    if (!files?.length || !sessionId) {
      if (!sessionId) toast.error(t('needSessionFirst'));
      return;
    }
    for (const file of Array.from(files).slice(0, MAX_ATTACHMENTS - attachments.length)) {
      try {
        const stored = await uploadAgentAttachment(sessionId, file);
        setAttachments((prev) => [
          ...prev,
          {
            attachment_id: stored.attachment_id,
            name: file.name,
            is_image: file.type.startsWith('image/'),
            size: stored.size,
            preview_url: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
          },
        ]);
      } catch (e: unknown) {
        toast.error(e instanceof Error ? e.message : t('uploadFailed'));
      }
    }
    if (fileRef.current) fileRef.current.value = '';
  };

  const ask = useMutation({
    mutationFn: async (question: string) => {
      let sid = sessionId;
      if (!sid) {
        // 草稿对话首条消息：此刻才创建真实会话（标题 = 问题前 60 字，
        // 绑定草稿项目目录与草稿检索范围）
        if (!draftProject) throw new Error(t('noProjectHint'));
        const resp = await api.POST('/api/v1/agent/sessions', {
          body: {
            title: question.slice(0, 60),
            project_path: draftProject,
            scope_space_ids: draftScope.length ? draftScope : undefined,
          },
        });
        const s = resp.data as unknown as { id?: string } | undefined;
        sid = s?.id;
        if (!sid) throw new Error(t('sessionCreateFailed'));
        // 抢在路由切换前置位：会话切换副作用见 streamingRef 守卫
        streamingRef.current = true;
        navigate(`/chat/${sid}`, { replace: true });
        void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
      }

      const pending = attachments;
      setAttachments([]);
      setMsgs((prev) => [
        ...prev,
        { role: 'user', content: question },
        { role: 'assistant', content: '', streaming: true, toolCalls: [] },
      ]);
      streamingRef.current = true;

      const updateLast = (fn: (m: AgentMessage) => AgentMessage) => {
        setMsgs((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.streaming) next[next.length - 1] = fn(last);
          return next;
        });
      };
      const upsertTool = (call: AgentToolCall) => {
        updateLast((m) => {
          const tools = [...(m.toolCalls ?? [])];
          const i = tools.findIndex((tc) => tc.call_id === call.call_id);
          if (i >= 0) tools[i] = { ...tools[i], ...call };
          else tools.push(call);
          return { ...m, toolCalls: tools };
        });
      };

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await streamAgentAnswer(
          sid,
          question,
          {
            onDelta: (text) => updateLast((m) => ({ ...m, content: m.content + text })),
            onReasoning: (text) => updateLast((m) => ({ ...m, reasoning: (m.reasoning ?? '') + text })),
            onToolStart: (call) => upsertTool(call),
            onToolEnd: (call) => upsertTool(call),
            onCitations: (citations, evidence) =>
              updateLast((m) => ({ ...m, citations, graphEvidence: evidence })),
            onUsage: (usage) => updateLast((m) => ({ ...m, usage })),
            onStatus: (status) => {
              if (status === 'cancelled') updateLast((m) => ({ ...m, streaming: false }));
            },
            onDone: (data) =>
              updateLast((m) => ({
                ...m,
                streaming: false,
                content: data.answer || m.content,
                citations: data.citations.length ? data.citations : m.citations,
                graphEvidence: data.graph_evidence.length ? data.graph_evidence : m.graphEvidence,
                usage: data.usage,
              })),
            onError: (message) => {
              setErrorMsg(message);
              updateLast((m) => ({ ...m, streaming: false }));
            },
          },
          pending.map((a) => a.attachment_id),
          controller.signal,
        );
      } finally {
        streamingRef.current = false;
        abortRef.current = null;
      }
      void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });
    },
    onError: (e) => {
      setErrorMsg(e instanceof Error ? e.message : t('generateFailedRetry'));
      setMsgs((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.streaming) next[next.length - 1] = { ...last, streaming: false };
        return next;
      });
    },
  });

  const stop = async () => {
    // 先断客户端流（停止渲染），再请求服务端终止 runtime（后台收尾 JSONL）
    abortRef.current?.abort();
    abortRef.current = null;
    if (sessionId) {
      try {
        await cancelAgentTurn(sessionId);
      } catch {
        toast.error(t('cancelFailed'));
      }
    }
  };

  const submit = () => {
    const q = input.trim();
    if (!q || ask.isPending || (!sessionId && !draftProject)) return;
    setInput('');
    setErrorMsg(null);
    ask.mutate(q);
  };

  const currentSession = (sessions.data?.items ?? []).find((s) => s.session_id === sessionId);
  // 草稿态检索范围用本地选择，真实会话沿用「本地覆盖优先」
  const scopeIds = sessionId
    ? (scopeOverride ?? currentSession?.scope_space_ids ?? [])
    : draftScope;
  const displayProject = sessionId ? currentSession?.project_path : draftProject;
  const spaceOptions = [
    ...(spaces.data ?? []).map((s) => ({ value: s.id, label: s.name })),
    ...(publicSpaces.data ?? [])
      .filter((s) => s.linked)
      .map((s) => ({ value: s.id, label: t('publicOption', { name: s.name }) })),
  ];

  return (
    <div className="flex h-svh min-w-0 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-background px-4 py-3">
        <h1 className="truncate text-base font-semibold">
          {currentSession?.title || t('defaultTitle')}
        </h1>
        {(sessionId || draftProject) && (
          <div className="flex items-center gap-2">
            {!!displayProject && (
              <span
                className="flex max-w-56 items-center gap-1 truncate rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                title={t('projectBadgeTitle', { path: displayProject })}
              >
                <FolderOpen className="size-3 shrink-0" />
                {displayProject}
              </span>
            )}
            <span className="shrink-0 text-sm text-muted-foreground">{t('scopeLabel')}</span>
            <MultiSelect
              value={scopeIds}
              options={spaceOptions}
              placeholder={t('scopePlaceholder')}
              loading={spaces.isLoading || publicSpaces.isLoading || sessions.isLoading}
              onChange={(v) => {
                if (sessionId) {
                  setScopeOverride(v);
                  patchSession.mutate({ scope_space_ids: v });
                } else {
                  setDraftScope(v); // 草稿态：随首条消息一并提交
                }
              }}
            />
          </div>
        )}
      </div>

      {(sessionId || draftProject) && scopeIds.length === 0 && !agentDisabled && (
        <div className="flex items-center gap-2 border-b bg-muted/40 px-4 py-2 text-sm text-muted-foreground">
          <ShieldAlert className="size-4 shrink-0 text-amber-600" />
          {t('noScopeHint')}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-4">
          {agentDisabled && (
            <div className="flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              <ShieldAlert className="size-4" />
              {t('unavailable', { reason: agentStatus.data?.reason ?? t('serviceUnreachable') })}
            </div>
          )}
          {msgs.length === 0 && !ask.isPending && !agentDisabled && (
            <EmptyState
              title={t('startTitle')}
              description={sessionId || draftProject ? t('emptyDesc') : t('noProjectHint')}
            />
          )}
          {msgs.map((m, i) => (
            <div key={i} className={m.role === 'user' ? 'flex justify-end' : ''}>
              <div
                className={`rounded-lg px-3 py-2 text-sm ${
                  m.role === 'user'
                    ? 'max-w-[85%] bg-primary text-primary-foreground'
                    : 'w-full bg-muted'
                }`}
              >
                {/* 思维链折叠面板（reasoning-delta） */}
                {m.role === 'assistant' && !!m.reasoning && (
                  <Collapsible className="mb-2">
                    <CollapsibleTrigger className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                      <Brain className="size-3" />
                      {m.streaming ? t('thinking') : t('thoughtProcess')}
                      <ChevronDown className="size-3" />
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <p className="mt-1 max-h-48 overflow-y-auto rounded bg-background/60 p-2 text-xs whitespace-pre-wrap text-muted-foreground">
                        {m.reasoning}
                      </p>
                    </CollapsibleContent>
                  </Collapsible>
                )}

                {/* 工具调用过程卡（tool_start/tool_end） */}
                {m.role === 'assistant' && (m.toolCalls?.length ?? 0) > 0 && (
                  <div className="mb-2 space-y-1">
                    {m.toolCalls!.map((tc) => (
                      <div
                        key={tc.call_id}
                        className="flex items-center gap-2 rounded border border-border/60 bg-background/60 px-2 py-1 text-xs"
                      >
                        <Wrench className="size-3 shrink-0 text-muted-foreground" />
                        <span className="truncate font-mono">
                          {tc.tool === 'mcp__loomvec__search_knowledge'
                            ? t('toolSearchKnowledge')
                            : tc.tool}
                        </span>
                        {tc.ok === undefined ? (
                          <Spinner className="size-3" />
                        ) : tc.ok ? (
                          <span className="shrink-0 text-emerald-600">{t('toolOk')}</span>
                        ) : (
                          <span className="shrink-0 text-destructive">{t('toolFailed')}</span>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {m.role === 'assistant' ? (
                  m.streaming && !m.content ? (
                    /* 等待首 token：动态加载图标（区别于流式中的淡入正文） */
                    <div className="flex items-center gap-2 text-muted-foreground">
                      <Spinner className="size-4" />
                      {t('thinking')}
                    </div>
                  ) : (
                    <AnswerMarkdown content={m.content} citations={m.citations} streaming={m.streaming} />
                  )
                ) : (
                  m.content
                )}

                {/* 引用列表（默认折叠，渲染与跳转沿用旧链路） */}
                {m.role === 'assistant' && (m.citations?.length ?? 0) > 0 && (
                  <Collapsible className="mt-2 border-t pt-2">
                    <CollapsibleTrigger className="group flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">
                      <ChevronDown className="size-3 transition-transform group-data-[state=open]:rotate-180" />
                      {t('citationsCount', { count: m.citations!.length })}
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <ul className="space-y-1 pt-2">
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
                    </CollapsibleContent>
                  </Collapsible>
                )}

                {/* 步级用量折叠条（cache_hit_rate / tokens） */}
                {m.role === 'assistant' && !m.streaming && m.usage && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    {t('usageLine', {
                      tokens:
                        (m.usage.output_tokens ?? 0) +
                        (m.usage.cache_read_tokens ?? 0) +
                        (m.usage.cache_write_tokens ?? 0) +
                        (m.usage.uncached_input_tokens ?? 0),
                      hit: Math.round((m.usage.cache_hit_rate ?? 0) * 100),
                      steps: m.usage.steps ?? 0,
                    })}
                  </p>
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
                {t('action.retry', { ns: 'ui' })}
              </button>
            </p>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* 输入区：附件 chips + 输入框 + 发送/停止。
          P5.6 起新对话自侧栏项目内创建（目录在创建时绑定），
          无会话态输入框置灰并提示先去侧栏新建 */}
      <div className="border-t bg-background px-4 py-3">
        <div className="mx-auto w-full max-w-3xl space-y-2">
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {attachments.map((a) => (
                <span
                  key={a.attachment_id}
                  className="flex items-center gap-1 rounded-full border bg-muted/60 px-2 py-0.5 text-xs"
                >
                  {a.is_image && a.preview_url ? (
                    <img src={a.preview_url} alt={a.name} className="size-4 rounded object-cover" />
                  ) : (
                    <FileUp className="size-3" />
                  )}
                  <span className="max-w-40 truncate">{a.name}</span>
                  <button
                    type="button"
                    aria-label={t('removeAttachment')}
                    onClick={() =>
                      setAttachments((prev) => prev.filter((x) => x.attachment_id !== a.attachment_id))
                    }
                  >
                    <X className="size-3 hover:text-destructive" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              accept="image/png,image/jpeg,image/webp,image/gif,text/plain,text/markdown,application/pdf,application/json,text/csv"
              onChange={(e) => void onFiles(e.target.files)}
            />
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('addAttachment')}
              title={t('needSessionFirst')}
              disabled={!sessionId || ask.isPending || attachments.length >= MAX_ATTACHMENTS}
              onClick={() => fileRef.current?.click()}
            >
              <Paperclip className="size-4" />
            </Button>
            <Input
              placeholder={
                agentDisabled
                  ? t('inputUnavailable')
                  : !sessionId && !draftProject
                    ? t('noProjectHint')
                    : t('inputPlaceholder')
              }
              value={input}
              disabled={agentDisabled || (!sessionId && !draftProject)}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.nativeEvent.isComposing) submit();
              }}
            />
            {ask.isPending ? (
              <Button size="icon" variant="destructive" aria-label={t('stop')} onClick={() => void stop()}>
                <Square className="size-4" />
              </Button>
            ) : (
              <Button size="icon" disabled={agentDisabled || !input.trim()} onClick={submit}>
                <Send className="size-4" />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
