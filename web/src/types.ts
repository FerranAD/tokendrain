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
  task_log?: string;
  usage?: { start: UsageWindow[]; end: UsageWindow[] };
}

export interface Project {
  id: string;
  name: string;
  description: string;
  task_log: string;
  next_run_feedback: string;
  status: string;
  default_model: string;
  default_reasoning_effort: string;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  latest_report?: Report | null;
  environment_metadata?: Record<string, unknown>;
  workspace_metadata?: Record<string, unknown>;
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
  report?: Report | null;
  thread_id?: string | null;
}

export interface Run {
  id: string;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  parallel: boolean;
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
  checks: { name: string; ok: boolean; message: string }[];
  uptime_seconds?: number;
  active_executions?: number;
}

export interface OpenAIStatus {
  connected: boolean;
  method?: string;
  account_label?: string;
  login?: { id: string; url: string; status: string; error?: string };
}

export interface Installation {
  id: string;
  account: string;
  permissions?: Record<string, string>;
}

export interface GitHubStatus {
  configured: boolean;
  app_id?: string;
  app_slug?: string;
  installation_url?: string;
  installations: Installation[];
}

export interface Repository {
  id: number;
  full_name: string;
  private?: boolean;
  default_branch?: string;
}

export interface ProjectGitHub {
  installation_id: string;
  repository_id: number;
  repository_name: string;
  permissions: Record<string, string>;
}

export interface Secret {
  name: string;
  description: string;
  updated_at?: string;
}

export interface StorageInfo {
  environment: { size_bytes: number; used_bytes: number; path?: string };
  workspace: { size_bytes: number; used_bytes: number; path?: string };
}

export interface Snapshot {
  id: string;
  name: string;
  created_at: string;
  environment_bytes?: number;
  workspace_bytes?: number;
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
