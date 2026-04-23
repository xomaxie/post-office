import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Bot, Mail, MailOpen, RefreshCcw, Send, Stamp, TriangleAlert, Wifi, WifiOff } from 'lucide-react';
import { archiveThread, dispatchQueued, failRun, fetchStatus, fetchThread, fetchThreads, markThreadRead, openEvents, purgeThread, submitTask } from './api';
import type { LiveState, MailMessage, StatusPayload, ThreadPayload, ThreadRecord, ThreadStatus } from './types';

const statusTone: Record<ThreadStatus, string> = {
  queued: 'text-brass',
  started: 'text-sky-300',
  completed: 'text-emerald-300',
  needs_input: 'text-amber-300',
  failed: 'text-rose-300',
};

const statusLabel: Record<ThreadStatus, string> = {
  queued: 'Queued',
  started: 'Running',
  completed: 'Done',
  needs_input: 'Blocked',
  failed: 'Failed',
};

const liveTone: Record<LiveState, string> = {
  connecting: 'text-amber-200',
  live: 'text-emerald-200',
  offline: 'text-rose-200',
};

const liveLabel: Record<LiveState, string> = {
  connecting: 'Connecting',
  live: 'Live',
  offline: 'Offline',
};

const statusOptions = [
  { value: '', label: 'All' },
  { value: 'queued', label: 'Queued' },
  { value: 'started', label: 'Running' },
  { value: 'needs_input', label: 'Blocked' },
  { value: 'completed', label: 'Done' },
  { value: 'failed', label: 'Failed' },
];

function formatDate(value: string | null | undefined) {
  if (!value) return '—';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value));
}

function excerpt(text: string, max = 132) {
  const compact = text.replace(/\s+/g, ' ').trim();
  return compact.length > max ? `${compact.slice(0, max - 1)}…` : compact;
}

function splitMessages(messages: MailMessage[]) {
  const request = [...messages].reverse().find((item) => item.author === 'agent-zero' && item.role === 'request');
  const mailbox = messages.filter((item) => item.author !== 'agent-zero');
  return { request, mailbox };
}

export default function App() {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [threads, setThreads] = useState<ThreadRecord[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState('');
  const [selectedThread, setSelectedThread] = useState<ThreadPayload | null>(null);
  const [subject, setSubject] = useState('New task');
  const [message, setMessage] = useState('Investigate, act autonomously, and only ask for help if blocked.');
  const [recipient, setRecipient] = useState('agent-fast');
  const [threadFilter, setThreadFilter] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [liveState, setLiveState] = useState<LiveState>('connecting');
  const eventSourceRef = useRef<EventSource | null>(null);

  async function loadDashboard(targetThreadId?: string) {
    setError('');
    try {
      const [statusData, threadData] = await Promise.all([fetchStatus(), fetchThreads(threadFilter, showArchived)]);
      setStatus(statusData);
      setThreads(threadData);
      const activeId = targetThreadId ?? selectedThreadId ?? threadData[0]?.id ?? '';
      if (activeId === '') {
        setSelectedThreadId('');
        setSelectedThread(null);
        return;
      }
      setSelectedThreadId(activeId);
      const threadPayload = await fetchThread(activeId);
      setSelectedThread(threadPayload);
      if (subject === '' && threadPayload.thread.subject) setSubject(threadPayload.thread.subject);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Failed to load');
    }
  }

  useEffect(() => {
    void loadDashboard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setLiveState('connecting');
    const source = openEvents({
      onOpen: () => setLiveState('live'),
      onReady: () => {
        setLiveState('live');
        void loadDashboard();
      },
      onChange: () => {
        setLiveState('live');
        void loadDashboard();
      },
      onError: () => setLiveState('offline'),
    });
    eventSourceRef.current = source;
    return () => {
      source.close();
      if (eventSourceRef.current === source) {
        eventSourceRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadFilter, showArchived]);

  const compositionMode = useMemo(() => (selectedThread ? 'Reply' : 'New'), [selectedThread]);
  const currentRequest = selectedThread ? splitMessages(selectedThread.messages).request : null;
  const mailboxMessages = selectedThread ? splitMessages(selectedThread.messages).mailbox : [];
  const unreadCount = mailboxMessages.filter((item) => item.read_at === null).length;

  async function handleSubmit() {
    if (message.trim() === '') return;
    setBusy(true);
    setError('');
    try {
      const result = await submitTask({
        subject,
        message,
        recipient,
        thread_id: selectedThreadId || undefined,
      });
      setMessage('');
      await loadDashboard(result.thread_id);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Failed to send');
    } finally {
      setBusy(false);
    }
  }

  async function handleDispatch() {
    setBusy(true);
    setError('');
    try {
      await dispatchQueued(3);
      await loadDashboard();
    } catch (dispatchError) {
      setError(dispatchError instanceof Error ? dispatchError.message : 'Dispatch failed');
    } finally {
      setBusy(false);
    }
  }

  async function handleMarkRead() {
    if (selectedThreadId === '') return;
    setBusy(true);
    setError('');
    try {
      await markThreadRead(selectedThreadId);
      await loadDashboard(selectedThreadId);
    } catch (markError) {
      setError(markError instanceof Error ? markError.message : 'Failed to mark read');
    } finally {
      setBusy(false);
    }
  }


  async function handleArchiveThread() {
    if (selectedThreadId === '') return;
    setBusy(true);
    setError('');
    try {
      await archiveThread(selectedThreadId, true);
      setSelectedThreadId('');
      setSelectedThread(null);
      await loadDashboard();
    } catch (archiveError) {
      setError(archiveError instanceof Error ? archiveError.message : 'Failed to archive');
    } finally {
      setBusy(false);
    }
  }

  async function handlePurgeThread() {
    if (selectedThreadId === '' || !window.confirm('Purge this thread permanently?')) return;
    setBusy(true);
    setError('');
    try {
      await purgeThread(selectedThreadId);
      setSelectedThreadId('');
      setSelectedThread(null);
      await loadDashboard();
    } catch (purgeError) {
      setError(purgeError instanceof Error ? purgeError.message : 'Failed to purge');
    } finally {
      setBusy(false);
    }
  }

  async function handleFailRun(runId: string) {
    setBusy(true);
    setError('');
    try {
      await failRun(runId);
      await loadDashboard(selectedThreadId || undefined);
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : 'Failed to stop run');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen bg-ink text-paper">
      <div className="pointer-events-none fixed inset-0 bg-grain opacity-90" />
      <div className="pointer-events-none fixed inset-0 bg-[linear-gradient(rgba(247,239,227,0.08)_1px,transparent_1px),linear-gradient(90deg,rgba(247,239,227,0.08)_1px,transparent_1px)] bg-[size:42px_42px] opacity-[0.08]" />

      <main className="relative mx-auto flex min-h-screen max-w-[1720px] flex-col px-4 pb-8 pt-4 sm:px-6 lg:px-8">
        <section className="border border-paper/20 bg-black/20 p-6 backdrop-blur-sm">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <h1 className="font-display text-[clamp(3rem,8vw,6.4rem)] uppercase leading-[0.9] tracking-[-0.05em] text-paper">
              Post Office
            </h1>
            <div className="grid gap-2 text-right font-mono text-xs uppercase tracking-[0.24em] text-paper/55">
              <div>{status?.worker_name ?? 'agent-fast'}</div>
              <div className={`${liveTone[liveState]} inline-flex items-center justify-end gap-2`}>
                {liveState === 'live' ? <Wifi className="h-4 w-4" /> : <WifiOff className="h-4 w-4" />}
                <span>{liveLabel[liveState]}</span>
              </div>
              <div>
                {status?.worker_mode ?? 'openai'}
                {status?.reasoning_effort ? ` · ${status.reasoning_effort}` : ''}
              </div>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 font-mono text-xs uppercase tracking-[0.24em] text-paper/45">
            <span>{status?.unread_messages ?? 0} unread</span>
            <span>{status?.recent_runs.length ?? 0} recent runs</span>
            <span>{status?.model_name ?? 'gpt-5.5'}</span>
            <span>shell {status?.worker_capabilities?.shell_access ? 'yes' : 'no'}</span>
            <span>gh {status?.worker_capabilities?.gh_cli_access ? 'yes' : 'no'}</span>
            <span>web {status?.worker_capabilities?.web_search_access ? 'yes' : 'aware'}</span>
          </div>
        </section>

        <section className="mt-4 grid min-h-0 flex-1 gap-4 xl:grid-cols-[400px_minmax(320px,480px)_minmax(0,1fr)]">
          <Panel className="flex flex-col">
            <SectionHeader
              title={compositionMode}
              right={
                <button
                  onClick={() => {
                    setSelectedThreadId('');
                    setSelectedThread(null);
                    setSubject('New task');
                  }}
                  className="text-xs uppercase tracking-[0.28em] text-paper/55 transition hover:text-paper"
                >
                  New
                </button>
              }
            />

            <Field label="To">
              <input
                value={recipient}
                onChange={(event) => setRecipient(event.target.value)}
                className="h-12 w-full border border-paper/15 bg-paper/5 px-4 font-mono text-sm text-paper outline-none transition focus:border-rust"
              />
            </Field>

            <Field label="Subject">
              <input
                value={subject}
                onChange={(event) => setSubject(event.target.value)}
                className="h-12 w-full border border-paper/15 bg-paper/5 px-4 font-display text-lg tracking-[-0.03em] text-paper outline-none transition focus:border-rust"
              />
            </Field>

            <Field label="Message" className="flex-1">
              <textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                rows={10}
                className="min-h-[220px] w-full resize-none border border-paper/15 bg-paper/5 px-4 py-4 text-base leading-relaxed text-paper outline-none transition focus:border-rust"
              />
            </Field>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <ActionButton onClick={() => void handleSubmit()} disabled={busy} icon={<Send className="h-4 w-4" />}>
                {busy ? 'Sending…' : selectedThread ? 'Reply' : 'Send'}
              </ActionButton>
              <ActionButton onClick={() => void handleDispatch()} disabled={busy} tone="secondary" icon={<RefreshCcw className="h-4 w-4" />}>
                Dispatch
              </ActionButton>
            </div>

            {currentRequest && (
              <div className="mt-6 border-t border-paper/15 pt-5">
                <div className="mb-2 flex items-center justify-between text-xs uppercase tracking-[0.28em] text-paper/45">
                  <span>Last</span>
                  <span>{formatDate(currentRequest.created_at)}</span>
                </div>
                <p className="text-sm leading-6 text-paper/72">{excerpt(currentRequest.body, 220)}</p>
              </div>
            )}
          </Panel>

          <Panel className="flex flex-col">
            <SectionHeader
              title="Threads"
              right={
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setShowArchived((value) => !value)}
                    className="border border-paper/15 px-3 py-2 font-mono text-[11px] uppercase tracking-[0.24em] text-paper/70 transition hover:text-paper"
                  >
                    {showArchived ? 'Hide archived' : 'Show archived'}
                  </button>
                  <select
                    value={threadFilter}
                    onChange={(event) => setThreadFilter(event.target.value)}
                    className="border border-paper/15 bg-transparent px-3 py-2 font-mono text-xs uppercase tracking-[0.24em] text-paper/75 outline-none"
                  >
                    {statusOptions.map((option) => (
                      <option key={option.value} value={option.value} className="bg-ink">
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
              }
            />

            <div className="mt-5 flex-1 overflow-auto pr-1">
              <div className="divide-y divide-paper/10 border-y border-paper/10">
                {threads.map((thread) => {
                  const isActive = thread.id === selectedThreadId;
                  return (
                    <button
                      key={thread.id}
                      onClick={() => {
                        setSelectedThreadId(thread.id);
                        void loadDashboard(thread.id);
                      }}
                      className={`w-full px-0 text-left transition ${isActive ? 'bg-paper/8' : 'hover:bg-paper/5'}`}
                    >
                      <div className="grid gap-3 px-4 py-4">
                        <div className="flex items-start justify-between gap-4">
                          <div>
                            <div className="font-display text-2xl tracking-[-0.04em] text-paper">{thread.subject}</div>
                            <div className="mt-2 font-mono text-xs uppercase tracking-[0.26em] text-paper/45">{thread.recipient}</div>
                          </div>
                          <div className={`font-mono text-xs uppercase tracking-[0.26em] ${statusTone[thread.status]}`}>
                            {statusLabel[thread.status]}
                          </div>
                        </div>
                        <div className="flex items-center justify-between gap-3 text-sm text-paper/50">
                          <span>{thread.last_error ? excerpt(thread.last_error, 64) : '—'}</span>
                          <span className="font-mono uppercase">{formatDate(thread.updated_at)}</span>
                        </div>
                      </div>
                    </button>
                  );
                })}
                {threads.length === 0 && <EmptyLine text="No threads." />}
              </div>
            </div>
          </Panel>

          <Panel className="flex min-h-[680px] flex-col">
            <SectionHeader
              title={selectedThread?.thread.subject ?? 'Thread'}
              right={
                selectedThread && (
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => void handleMarkRead()}
                      disabled={busy || unreadCount === 0}
                      className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-paper/55 transition hover:text-paper disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      <MailOpen className="h-4 w-4" />
                      Read
                    </button>
                    <button
                      onClick={() => void handleArchiveThread()}
                      disabled={busy}
                      className="text-xs uppercase tracking-[0.24em] text-paper/55 transition hover:text-paper disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      Archive
                    </button>
                    <button
                      onClick={() => void handlePurgeThread()}
                      disabled={busy}
                      className="text-xs uppercase tracking-[0.24em] text-rose-200 transition hover:text-rose-100 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      Purge
                    </button>
                  </div>
                )
              }
            />

            {selectedThread ? (
              <>
                <div className="mt-5 flex flex-wrap gap-x-6 gap-y-2 font-mono text-xs uppercase tracking-[0.24em] text-paper/45">
                  <span>{selectedThread.thread.recipient}</span>
                  <span>{unreadCount} unread</span>
                  <span>{selectedThread.runs.length} runs</span>
                </div>

                <div className="mt-5 border border-paper/12">
                  {selectedThread.runs.map((run) => (
                    <div key={run.id} className="grid gap-3 border-b border-paper/10 px-4 py-4 last:border-b-0 lg:grid-cols-[1fr_140px_140px_70px]">
                      <div>
                        <div className={`font-display text-xl tracking-[-0.04em] ${statusTone[run.state]}`}>{statusLabel[run.state]}</div>
                        <div className="mt-2 font-mono text-[11px] uppercase tracking-[0.26em] text-paper/42">{run.id}</div>
                        {run.activity_text ? <div className="mt-2 text-sm text-paper/60">{excerpt(run.activity_text, 88)}</div> : null}
                        {run.error_text ? <div className="mt-2 text-sm text-paper/58">{excerpt(run.error_text, 88)}</div> : null}
                        {run.worker_pid ? <div className="mt-2 font-mono text-[11px] text-paper/42">pid {run.worker_pid}</div> : null}
                        {run.heartbeat_at ? (
                          <div className="mt-2 font-mono text-[11px] text-paper/42">heartbeat {formatDate(run.heartbeat_at)}</div>
                        ) : null}
                        {run.workdir ? <div className="mt-2 text-xs text-paper/42">{run.workdir}</div> : null}
                        {run.artifacts.length > 0 ? (
                          <div className="mt-3 flex flex-wrap gap-2">
                            {run.artifacts.map((artifact) => (
                              <a
                                key={artifact.path}
                                href={artifact.url ?? '#'}
                                target="_blank"
                                rel="noreferrer"
                                className={`border px-2 py-1 font-mono text-[11px] uppercase tracking-[0.24em] transition ${
                                  artifact.url
                                    ? 'border-paper/15 text-paper/60 hover:text-paper'
                                    : 'cursor-default border-paper/10 text-paper/25'
                                }`}
                                onClick={(event) => {
                                  if (!artifact.url) event.preventDefault();
                                }}
                                title={artifact.path}
                              >
                                {artifact.name}
                              </a>
                            ))}
                          </div>
                        ) : null}
                      </div>
                      <MetaCell label="Start" value={formatDate(run.started_at ?? run.created_at)} />
                      <MetaCell label="End" value={formatDate(run.finished_at)} />
                      <div className="flex items-start justify-end">
                        {run.state === 'started' ? (
                          <button
                            onClick={() => void handleFailRun(run.id)}
                            disabled={busy}
                            className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-rose-200 transition hover:text-rose-100 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            <TriangleAlert className="h-3.5 w-3.5" />
                            Stop
                          </button>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>

                <div className="mt-5 flex-1 overflow-auto border border-paper/12">
                  {mailboxMessages.length > 0 ? (
                    mailboxMessages.map((item) => (
                      <article key={item.id} className="border-b border-paper/10 px-5 py-5 last:border-b-0">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div className="flex items-center gap-3">
                            <div className="flex h-10 w-10 items-center justify-center border border-paper/15 bg-paper/5 text-rust">
                              {item.author === 'postmaster' ? <Stamp className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
                            </div>
                            <div>
                              <div className="font-display text-2xl tracking-[-0.04em] text-paper">{item.author}</div>
                              <div className="font-mono text-[11px] uppercase tracking-[0.28em] text-paper/42">
                                {item.role} · {statusLabel[item.status]}
                              </div>
                            </div>
                          </div>
                          <div className="text-right font-mono text-[11px] uppercase tracking-[0.26em] text-paper/38">
                            <div>{formatDate(item.created_at)}</div>
                            <div className={item.read_at ? 'text-paper/32' : 'text-brass'}>{item.read_at ? 'Read' : 'Unread'}</div>
                          </div>
                        </div>
                        <pre className="mt-4 whitespace-pre-wrap font-sans text-[15px] leading-7 text-paper/78">{item.body}</pre>
                      </article>
                    ))
                  ) : (
                    <EmptyLine text="No mail." />
                  )}
                </div>
              </>
            ) : (
              <div className="mt-5 flex flex-1 items-center justify-center border border-dashed border-paper/15 text-center">
                <div>
                  <div className="mx-auto flex h-16 w-16 items-center justify-center border border-paper/15 bg-paper/5 text-rust">
                    <Mail className="h-7 w-7" />
                  </div>
                  <h2 className="mt-5 font-display text-4xl tracking-[-0.05em] text-paper">No thread</h2>
                </div>
              </div>
            )}
          </Panel>
        </section>

        {error && (
          <div className="mt-4 flex items-center gap-3 border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
            <TriangleAlert className="h-4 w-4" />
            <span>{error}</span>
          </div>
        )}
      </main>
    </div>
  );
}

function Panel({ className = '', children }: { className?: string; children: ReactNode }) {
  return <section className={`border border-paper/15 bg-black/20 p-5 backdrop-blur-sm ${className}`}>{children}</section>;
}

function SectionHeader({ title, right }: { title: string; right?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-paper/12 pb-4">
      <h2 className="font-display text-[clamp(1.8rem,4vw,3rem)] tracking-[-0.05em] text-paper">{title}</h2>
      {right}
    </div>
  );
}

function Field({ label, children, className = '' }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={`mt-4 block ${className}`}>
      <div className="mb-2 text-xs uppercase tracking-[0.28em] text-paper/50">{label}</div>
      {children}
    </label>
  );
}

function MetaCell({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="font-mono text-[11px] uppercase tracking-[0.28em] text-paper/42">{label}</div>
      <div className="mt-2 text-sm text-paper/72">{value}</div>
    </div>
  );
}

function EmptyLine({ text }: { text: string }) {
  return <div className="px-4 py-12 text-center text-sm text-paper/55">{text}</div>;
}

function ActionButton({
  children,
  onClick,
  disabled,
  tone = 'primary',
  icon,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: 'primary' | 'secondary';
  icon?: ReactNode;
}) {
  const classes =
    tone === 'primary'
      ? 'border-rust bg-rust text-ink hover:bg-[#ff7b36]'
      : 'border-paper/20 bg-paper/8 text-paper hover:bg-paper/14';
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex h-12 items-center justify-center gap-2 border px-4 text-sm uppercase tracking-[0.24em] transition disabled:cursor-not-allowed disabled:opacity-40 ${classes}`}
    >
      {icon}
      {children}
    </button>
  );
}
