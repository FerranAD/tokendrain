import { createContext, useContext } from 'react';
import type { AnchorHTMLAttributes, ReactNode } from 'react';
import type { Report, StopCondition, UsageWindow } from './types';

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
  title: string;
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
        ↘
      </span>
      <h3>{title}</h3>
      {children && <p className="muted">{children}</p>}
      {action}
    </div>
  );
}

export function Loading() {
  return (
    <div role="status" className="loading">
      <span className="spinner" /> Loading…
    </div>
  );
}
export function Badge({ status }: { status: string }) {
  return <span className={`badge status-${status}`}>{status.replaceAll('_', ' ')}</span>;
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
          className="usage-card"
          key={`${window.limit_id}-${window.window_minutes}-${index}`}
        >
          <div className="row between">
            <span className="small-label">{window.name || window.limit_id}</span>
            <span className="usage-value">
              {window.used_percent.toFixed(1)}
              <small>%</small>
            </span>
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
            <span>{duration(window.window_minutes)} window</span>
            <span>
              {window.resets_at ? `Resets ${date(window.resets_at)}` : 'Reset not supplied'}
            </span>
          </div>
        </article>
      ))}
    </div>
  );
}

export function ReportView({ report }: { report?: Report | null }) {
  if (!report)
    return (
      <Empty title="No report yet">
        The next execution will leave a structured summary of its work and any blockers.
      </Empty>
    );
  const sections = [
    { title: 'Completed', items: report.completed },
    { title: 'Remaining', items: report.remaining },
    { title: 'Blockers', items: report.blockers },
  ];
  return (
    <div className="report">
      <div className="row between">
        <h3>Run report</h3>
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
        {sections.map((section) => (
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
