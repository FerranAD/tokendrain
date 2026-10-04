import { Icon } from './icons';
import { createContext, useContext } from 'react';
import type { AnchorHTMLAttributes, ReactNode } from 'react';
import type { Execution, Report, StopCondition, UsageWindow } from './types';

export const Navigation = createContext({ path: '/', go: (_path: string) => {} });
export const useNavigation = () => useContext(Navigation);

export function Link({ href = '/', children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const { go } = useNavigation();
  return (
    <a
      href={href}
      {...props}
      onClick={(event) => {
        props.onClick?.(event);
        if (
          !event.defaultPrevented &&
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey &&
          href.startsWith('/')
        ) {
          event.preventDefault();
          go(href);
        }
      }}
    >
      {children}
    </a>
  );
}

export function PageTitle({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: ReactNode;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p className="muted">{description}</p>}
      </div>
      <div className="heading-actions">{actions}</div>
    </header>
  );
}

export function ErrorNotice({ error }: { error?: string }) {
  return error ? (
    <div role="alert" className="notice error">
      {error}
    </div>
  ) : null;
}

export function ActionNotice({ error, notice }: { error?: string; notice?: string }) {
  return (
    <>
      <ErrorNotice error={error} />
      {notice && (
        <div role="status" className="notice success">
          {notice}
        </div>
      )}
    </>
  );
}

export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-mark" aria-hidden="true">
        <Icon name="folder" />
      </span>
      <h3>{title}</h3>
      {children && <div className="empty-description muted">{children}</div>}
      {action}
    </div>
  );
}

export function Loading() {
  return (
    <div role="status" className="loading">
      <span className="spinner" />
      <span>Loading…</span>
      <span className="loading-bars" aria-hidden="true">
        <i />
        <i />
      </span>
    </div>
  );
}
export function Badge({ status }: { status: string }) {
  return (
    <span className={`badge status-${status}`}>
      <span className="badge-dot" aria-hidden="true" />
      {status.replaceAll('_', ' ')}
    </span>
  );
}
export function shortId(id: string) {
  return id.slice(0, 8);
}
export function date(value?: string | null) {
  return value
    ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : '—';
}
export function bytes(value: number) {
  if (!Number.isFinite(value)) return '—';
  if (value < 1024) return `${value} B`;
  const order = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** order).toFixed(1)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][order]}`;
}
export function duration(minutes?: number | null) {
  if (!minutes) return 'Provider window';
  if (minutes % 1440 === 0) return `${minutes / 1440} day${minutes === 1440 ? '' : 's'}`;
  if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? '' : 's'}`;
  return `${minutes} minutes`;
}
export function conditionLabel(condition: StopCondition) {
  if (condition.kind === 'usage')
    return `${duration(condition.window_minutes)} window ≥ ${condition.used_percent}%${condition.limit_id ? ` (${condition.limit_id})` : ''}`;
  if (condition.kind === 'elapsed') return `Elapsed time ≥ ${duration(condition.seconds / 60)}`;
  if (condition.kind === 'provider_limit') return 'Provider prevents further work';
  return 'Project is complete';
}

function resetIn(value: string) {
  const minutes = Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 60000));
  if (minutes === 0) return 'now';
  if (minutes >= 1440)
    return `in ${Math.floor(minutes / 1440)}d ${Math.floor((minutes % 1440) / 60)}h`;
  return `in ${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function UsageCards({ windows }: { windows: UsageWindow[] }) {
  if (!windows.length)
    return (
      <div className="usage-empty">
        <span className="dot" />
        Usage has not been observed yet. Connect OpenAI to load your current limits.
      </div>
    );
  return (
    <div className="usage-grid">
      {windows.map((window, index) => (
        <article
          className={`usage-card ${window.used_percent >= 90 ? 'near-limit' : ''}`}
          key={`${window.limit_id}-${window.window_minutes}-${index}`}
        >
          <div className="row between">
            <span className="small-label">
              <Icon name="usage" />
              {window.name ||
                (window.window_minutes === 10080 ? 'Weekly' : duration(window.window_minutes))}
            </span>
            <div className="usage-total">
              <span className="usage-value">
                {window.used_percent.toFixed(1)}
                <small>%</small>
              </span>
              <span className="usage-used">used</span>
            </div>
          </div>
          <div
            className="meter"
            role="progressbar"
            aria-label={`${window.name || window.limit_id} usage`}
            aria-valuenow={window.used_percent}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div
              className={window.used_percent >= 90 ? 'high' : ''}
              style={{ width: `${Math.max(0, Math.min(100, window.used_percent))}%` }}
            />
          </div>
          <div className="row between muted tiny">
            <span>
              {Math.max(0, 100 - window.used_percent).toFixed(1)}% left ·{' '}
              {duration(window.window_minutes)} window
            </span>
            <span className="usage-reset">
              <span>
                {window.resets_at ? `Resets ${resetIn(window.resets_at)}` : 'Reset not supplied'}
              </span>
              {window.resets_at && (
                <time dateTime={window.resets_at}>{date(window.resets_at)}</time>
              )}
            </span>
          </div>
        </article>
      ))}
    </div>
  );
}

export function ReportView({
  report,
  title = 'Last agent checkpoint',
}: {
  report?: Report | null;
  title?: string;
}) {
  if (!report)
    return <Empty title="No report yet">No valid agent checkpoint has been produced yet.</Empty>;
  const sections = [
    { title: 'Completed', items: report.completed },
    { title: 'Remaining', items: report.remaining },
    { title: 'Blockers', items: report.blockers },
  ];
  return (
    <div className="report">
      <div className="row between">
        <h3>{title}</h3>
        <Badge status={report.status} />
      </div>
      <p className="preserve">{report.summary}</p>
      {report.changes && (
        <div className="change-summary">
          <span>
            <strong>{report.changes.files_changed}</strong> files changed
          </span>
          <span className="positive">+{report.changes.insertions}</span>
          <span className="negative">−{report.changes.deletions}</span>
          {!!report.changes.commits?.length && <span>{report.changes.commits.length} commits</span>}
        </div>
      )}
      <div className="report-columns">
        {sections
          .filter((section) => section.items?.length)
          .map((section) => (
            <section key={section.title}>
              <h4>{section.title}</h4>
              {section.items?.length ? (
                <ul className="plain-list">
                  {section.items.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">None reported.</p>
              )}
            </section>
          ))}
      </div>
      {report.suggested_next_action && (
        <div className="callout">
          <strong>Suggested next action</strong>
          <p>{report.suggested_next_action}</p>
        </div>
      )}
      {!!report.changes?.commits?.length && (
        <details>
          <summary>Commits</summary>
          <ul className="plain-list">
            {report.changes.commits.map((commit) => (
              <li className="mono" key={commit}>
                {commit}
              </li>
            ))}
          </ul>
        </details>
      )}
      {!!report.usage?.end?.length && (
        <details>
          <summary>Usage at end of execution</summary>
          <UsageCards windows={report.usage.end} />
        </details>
      )}
    </div>
  );
}

export function ExecutionOutcome({ execution }: { execution?: Execution | null }) {
  if (!execution) return null;
  const reason = execution.termination_reason;
  const hard = execution.threshold_mode === 'hard';
  const label =
    reason === 'user_cancelled'
      ? 'Cancelled by user'
      : reason === 'usage_threshold'
        ? `Stopped — ${hard ? 'hard usage limit' : 'usage threshold'} reached`
        : reason === 'provider_limit'
          ? 'Stopped — provider limit reached'
          : reason === 'runtime_limit'
            ? 'Stopped — runtime limit reached'
            : reason === 'no_progress'
              ? 'Stopped — no progress'
              : reason === 'infrastructure_error'
                ? 'Infrastructure failure'
                : execution.status.replaceAll('_', ' ');
  return (
    <div className={`execution-outcome outcome-${execution.status}`}>
      <Icon
        name={
          execution.status === 'completed'
            ? 'checkpoint'
            : reason === 'usage_threshold'
              ? 'stop'
              : execution.status === 'failed'
                ? 'alert'
                : 'clock'
        }
      />
      <strong>{label.toUpperCase()}</strong>
      {execution.finished_at && (
        <p className="muted small">Run stopped {date(execution.finished_at)}</p>
      )}
      {execution.termination_detail && <p>{execution.termination_detail}</p>}
      {reason === 'usage_threshold' && (
        <p className="small">
          {hard
            ? execution.interrupted
              ? 'Active Codex work was interrupted when the configured limit was observed.'
              : 'No further model work was started after the limit was observed.'
            : execution.interrupted
              ? 'Graceful stop timed out; Codex was interrupted.'
              : 'Graceful stop completed.'}
        </p>
      )}
      {execution.interrupted && (
        <p className="small muted">
          Newer work was interrupted. The last valid agent checkpoint is retained below.
        </p>
      )}
      {execution.error && reason === 'infrastructure_error' && (
        <ErrorNotice error={execution.error} />
      )}
    </div>
  );
}
