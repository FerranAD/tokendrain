export interface UsageWindow {
  limit_id: string;
  name?: string | null;
  used_percent: number;
  window_minutes?: number | null;
  resets_at?: string | null;
  observed_at?: string;
}

export interface Report {
  status: string;
  summary: string;
  completed: string[];
  remaining: string[];
  blockers: string[];
  changes?: { files_changed: number; insertions: number; deletions: number; commits?: string[] };
  suggested_next_action?: string;

  usage?: { start: UsageWindow[]; end: UsageWindow[] };
}

export interface Project {
  id: string;
  name: string;
  description: string;

  next_run_feedback: string;
  status: string;
  default_model: string;
  default_reasoning_effort: string;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  latest_report?: Report | null;
  latest_execution?: Execution | null;
  storage_metadata?: Record<string, unknown>;
}

export type StopCondition =
  | { kind: 'usage'; window_minutes: number; used_percent: number; limit_id?: string }
  | { kind: 'elapsed'; seconds: number }
  | { kind: 'provider_limit' }
  | { kind: 'project_completed' };

export interface ProjectConfig {
  project_id: string;
  model: string;
  reasoning_effort: string;
}

export interface RunTemplate {
  projects: ProjectConfig[];
  stop_conditions: StopCondition[];
  parallel: boolean;
  threshold_mode?: 'graceful' | 'hard';
}

export interface Execution {
  id: string;
  project_id: string;
  project_name?: string;
  run_id: string;
  status: string;
  model: string;
  reasoning_effort: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  termination_reason?: string | null;
  termination_detail?: string | null;
  threshold_mode?: 'graceful' | 'hard' | null;
  interrupted?: boolean;
  report?: Report | null;
  checkpoint_from_execution_id?: string | null;
  thread_id?: string | null;
}

export interface Run {
  id: string;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  parallel: boolean;
  threshold_mode?: 'graceful' | 'hard';
  stop_conditions: StopCondition[];
  executions: Execution[];
}

export interface Schedule {
  id: string;
  name: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  run_template: RunTemplate;
  next_run_at?: string | null;
  last_run_at?: string | null;
}

export interface CodexModel {
  id: string;
  name?: string;
  reasoning_efforts?: string[];
  is_default?: boolean;
}

export interface SystemInfo {
  version: string;
  backend: string;
  concurrency: number;
  vm_defaults: { vcpus: number; memory_mib: number; disk_gib: number };
  checks: {
    name: string;
    ok: boolean;
    message: string;
    scope?: 'host' | 'daemon' | 'helper' | 'mock';
  }[];
  uptime_seconds?: number;
  active_executions?: number;
}

export interface OpenAIStatus {
  connected: boolean;
  valid?: boolean;
  credential_error?: string;
  usage_error?: string | null;
  connection_ok?: boolean | null;
  connection_checked_at?: number | null;
  reauth_required?: boolean;
  method?: string;
  account_label?: string;
  login?: { id: string; url: string; status: string; error?: string };
}

export type GitHubAccessMode = 'read_only' | 'pull_requests' | 'direct_write';

export interface GitHubStatus {
  configured: boolean;
  name?: string;
  repository_count: number;
  installation_url?: string;
  policy_errors?: string[];
}

export interface Repository {
  id: number;
  full_name: string;
  private?: boolean;
  default_branch?: string;
  used_by?: { project_id: string; project_name: string; access_mode: GitHubAccessMode }[];
  policy_error?: string | null;
}

export interface ProjectGitHub {
  repository_id: number;
  repository_name: string;
  access_mode: GitHubAccessMode;
  allow_workflows: boolean;
  policy_error?: string | null;
}

export interface Secret {
  name: string;
  description: string;
  updated_at?: string;
}

export interface StorageInfo {
  virtual_size_bytes: number;
  allocated_bytes: number;
}

export interface LiveEvent {
  id?: string | number;
  type: string;
  message?: string;
  timestamp?: string;
  execution_id?: string;
  run_id?: string;
  project_id?: string;
  data?: Record<string, unknown>;
}

export type TaskColumn = 'backlog' | 'todo' | 'in_progress' | 'done';
export interface Task {
  id: string;
  project_id: string;
  title: string;
  description: string;
  column: TaskColumn;
  position: number;
  origin: 'user' | 'agent';
}
