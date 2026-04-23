import type { EventPayload, StatusPayload, ThreadPayload, ThreadRecord } from './types';

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed with ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function fetchStatus(): Promise<StatusPayload> {
  return parseJson<StatusPayload>(await fetch('/api/status'));
}

export async function fetchThreads(status = '', includeArchived = false): Promise<ThreadRecord[]> {
  const url = new URL('/api/threads', window.location.origin);
  if (status) url.searchParams.set('status', status);
  if (includeArchived) url.searchParams.set('include_archived', 'true');
  url.searchParams.set('limit', '24');
  return parseJson<ThreadRecord[]>(await fetch(url));
}

export async function fetchThread(threadId: string): Promise<ThreadPayload> {
  return parseJson<ThreadPayload>(await fetch(`/api/threads/${threadId}`));
}

export async function submitTask(payload: {
  subject: string;
  message: string;
  recipient?: string;
  thread_id?: string;
  workdir?: string;
  metadata?: Record<string, unknown>;
}): Promise<{ thread_id: string; run_id: string; status: string }> {
  return parseJson(await fetch('/api/submit-task', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }));
}

export async function markThreadRead(threadId: string): Promise<void> {
  await parseJson(await fetch(`/api/threads/${threadId}/mark-read`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mailbox_only: true }),
  }));
}

export async function archiveThread(threadId: string, archived = true): Promise<void> {
  await parseJson(await fetch(`/api/threads/${threadId}/archive`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ archived }),
  }));
}

export async function purgeThread(threadId: string): Promise<void> {
  await parseJson(await fetch(`/api/threads/${threadId}/purge`, {
    method: 'POST',
  }));
}

export async function dispatchQueued(limit = 1): Promise<void> {
  await parseJson(await fetch('/api/dispatch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limit }),
  }));
}

export async function failRun(runId: string, reason = 'Stopped from UI'): Promise<void> {
  await parseJson(await fetch(`/api/runs/${runId}/fail`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }));
}

export function openEvents(handlers: {
  onReady?: (payload: EventPayload) => void;
  onChange?: (payload: EventPayload) => void;
  onError?: () => void;
  onOpen?: () => void;
}): EventSource {
  const source = new EventSource('/api/events');
  source.addEventListener('open', () => handlers.onOpen?.());
  source.addEventListener('ready', (event) => {
    handlers.onReady?.(JSON.parse((event as MessageEvent).data) as EventPayload);
  });
  source.addEventListener('change', (event) => {
    handlers.onChange?.(JSON.parse((event as MessageEvent).data) as EventPayload);
  });
  source.addEventListener('error', () => handlers.onError?.());
  return source;
}
