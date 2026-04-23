export type ThreadStatus = 'queued' | 'started' | 'completed' | 'needs_input' | 'failed';
export type LiveState = 'connecting' | 'live' | 'offline';

export interface ThreadRecord {
  id: string;
  recipient: string;
  subject: string;
  status: ThreadStatus;
  created_at: string;
  updated_at: string;
  last_run_id: string | null;
  last_error: string | null;
  archived_at: string | null;
}

export interface RunRecord {
  id: string;
  thread_id: string;
  recipient: string;
  state: ThreadStatus;
  created_at: string;
  started_at: string | null;
  heartbeat_at: string | null;
  finished_at: string | null;
  worker_pid: number | null;
  cancel_requested_at: string | null;
  activity_text: string | null;
  workdir: string | null;
  error_text: string | null;
  artifact_paths: string[];
  artifacts: ArtifactLink[];
}

export interface ArtifactLink {
  name: string;
  path: string;
  url: string | null;
}

export interface MailMessage {
  id: string;
  thread_id: string;
  run_id: string | null;
  author: string;
  role: string;
  status: ThreadStatus;
  body: string;
  created_at: string;
  read_at: string | null;
  metadata: Record<string, unknown>;
}

export interface ThreadPayload {
  thread: ThreadRecord;
  runs: RunRecord[];
  messages: MailMessage[];
}

export interface WorkerCapabilities {
  aware_tools: string[];
  installed_cli_tools: string[];
  shell_access: boolean;
  gh_cli_access: boolean;
  web_search_access: boolean;
  notes: string;
}

export interface StatusPayload {
  worker_mode: string;
  worker_name: string;
  model_name: string;
  reasoning_effort?: string;
  db_path: string;
  unread_messages: number;
  recent_threads: ThreadRecord[];
  recent_runs: RunRecord[];
  change_counter?: number;
  worker_capabilities?: WorkerCapabilities;
}

export interface EventPayload {
  seq: number;
  timestamp: string;
}
