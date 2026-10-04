import { useEffect, useRef, useState } from 'react';
import type { Execution, LiveEvent, Report } from './types';
import { ReportView } from './ui';

type Payload = {
  type?: string;
  phase?: string;
  text?: string;
  command?: string;
  cwd?: string;
  status?: string;
  exitCode?: number;
  durationMs?: number;
  aggregatedOutput?: string;
  stdout?: string;
  stderr?: string;
  changes?: { path: string; kind?: { type?: string }; diff?: string }[];
};

function normalize(event: LiveEvent): LiveEvent {
  if (event.type !== 'execution.log') return event;
  // Compatibility for persisted protocol events from older Runs.
  try {
    const raw = JSON.parse(event.message || '') as Payload;
    if (raw.type === 'userMessage') return { ...event, type: 'execution.debug' };
    if (raw.type === 'agentMessage') {
      if (raw.phase == null || raw.phase === 'final_answer') {
        try {
          const report = JSON.parse(raw.text || '') as Report;
          if (
            typeof report.summary === 'string' &&
            ['completed', 'blocked', 'failed', 'cancelled', 'in_progress'].includes(report.status)
          )
            return { ...event, type: 'agent.checkpoint', data: { raw, report } };
        } catch {
          /* Raw remains accessible. */
        }
        if (raw.phase === 'final_answer') return { ...event, type: 'execution.debug' };
      }
      return { ...event, type: 'agent.progress', message: raw.text, data: { raw } };
    }
    if (raw.type === 'commandExecution')
      return { ...event, type: 'command', data: { ...raw, raw } };
    if (raw.type === 'fileChange')
      return { ...event, type: 'files.changed', data: { ...raw, raw } };
    return { ...event, type: 'execution.debug' };
  } catch {
    return { ...event, type: 'execution.activity' };
  }
}

function ActivityCard({ event }: { event: LiveEvent }) {
  const data = (event.data || {}) as Payload & {
    report?: Report;
    mode?: string;
    windows?: { name?: string; used_percent: number }[];
  };
  if (event.type === 'agent.checkpoint' && data.report)
    return (
      <div className="activity-checkpoint">
        <ReportView report={data.report} />
      </div>
    );
  if (event.type === 'command') {
    const failed = data.status === 'failed' || (data.exitCode != null && data.exitCode !== 0);
    const output = data.aggregatedOutput || [data.stdout, data.stderr].filter(Boolean).join('\n');
    const http = output?.match(/HTTP\s+(\d{3})/i);
    return (
      <div className={failed ? 'command-card failed' : 'command-card'}>
        <strong>
          {failed
            ? 'Command failed'
            : data.status === 'completed'
              ? 'Command ✓'
              : `Command · ${data.status || 'finished'}`}
        </strong>
        <pre className="command-line">{data.command || event.message}</pre>
        <div className="muted tiny">
          {data.cwd}
          {data.exitCode != null && ` · Exit ${data.exitCode}`}
          {data.durationMs != null && ` · ${(data.durationMs / 1000).toFixed(2)} s`}
        </div>
        {failed && (
          <p className="negative small">
            {http
              ? `HTTP ${http[1]}${http[1] === '403' ? ' — repository write access unavailable' : ''}`
              : output?.split('\n').filter(Boolean).slice(-2).join('\n') ||
                'Command did not succeed.'}
          </p>
        )}
        {output && (
          <details>
            <summary>{failed ? 'Show full output' : 'Show output'}</summary>
            <pre className="activity-output">{output}</pre>
          </details>
        )}
      </div>
    );
  }
  if (event.type === 'files.changed')
    return (
      <div>
        <strong>Files changed</strong>
        {data.changes?.map((change, i) => (
          <div className="file-change" key={i}>
            <code>{change.path}</code>
            {change.kind?.type && <span className="muted tiny"> · {change.kind.type}</span>}
            {change.diff && (
              <details>
                <summary>Show diff</summary>
                <pre className="activity-output">{change.diff}</pre>
              </details>
            )}
          </div>
        ))}
      </div>
    );
  if (event.type === 'execution.usage_stop')
    return (
      <div className="callout warning">
        <strong>
          {data.mode === 'hard' ? 'Hard usage limit reached' : 'Usage threshold reached'}
        </strong>
        <p>{event.message}</p>
        <p className="small">
          {data.mode === 'hard'
            ? 'Interrupting active Codex work. No finalization turn.'
            : 'Asking the agent to wrap up and save a checkpoint.'}
        </p>
      </div>
    );
  if (event.type === 'usage.updated')
    return (
      <div>
        <strong>Usage updated</strong>
        <p className="small muted">
          {data.windows
            ?.map((w) => `${w.name || 'Provider window'}: ${w.used_percent.toFixed(1)}% used`)
            .join(' · ')}
        </p>
      </div>
    );
  const label =
    event.type === 'agent.progress'
      ? 'Agent'
      : event.type === 'execution.state'
        ? 'Execution'
        : event.type.includes('error')
          ? 'Error'
          : 'Activity';
  return (
    <div>
      <strong className={label === 'Error' ? 'negative' : ''}>{label}</strong>
      <p className="preserve activity-text">{event.message || event.type.replaceAll('.', ' ')}</p>
    </div>
  );
}

export function ActivityTimeline({
  events,
  executions,
}: {
  events: LiveEvent[];
  executions: Execution[];
}) {
  const [project, setProject] = useState('all');
  const [raw, setRaw] = useState(false);
  const [following, setFollowing] = useState(true);
  const viewport = useRef<HTMLDivElement>(null);
  const normalized = events.map(normalize).filter((e, i, all) => {
    if (raw || e.type !== 'agent.checkpoint') return true;
    const last = all
      .slice(0, i)
      .reverse()
      .find(
        (previous) =>
          previous.execution_id === e.execution_id && previous.type === 'agent.checkpoint',
      );
    const checkpointKey = (report: unknown) => {
      if (!report || typeof report !== 'object') return JSON.stringify(report);
      const { usage: _usage, ...checkpoint } = report as Report;
      return JSON.stringify(checkpoint);
    };
    return !last || checkpointKey(last.data?.report) !== checkpointKey(e.data?.report);
  });
  const visible = normalized.filter(
    (e) =>
      (project === 'all' ||
        e.project_id === project ||
        executions.some((x) => x.project_id === project && x.id === e.execution_id)) &&
      (raw || e.type !== 'execution.debug'),
  );
  useEffect(() => {
    if (following && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [events, following, project, raw]);
  return (
    <>
      <div className="row between wrap activity-controls">
        <label>
          Project
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="all">All</option>
            {executions.map((x) => (
              <option key={x.id} value={x.project_id}>
                {x.project_name || x.project_id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={raw} onChange={(e) => setRaw(e.target.checked)} />
          Raw events
        </label>
      </div>
      <div
        ref={viewport}
        className="activity-timeline"
        role="log"
        onScroll={() => {
          const el = viewport.current;
          if (el) setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 70);
        }}
      >
        {visible.map((e, i) => {
          const execution = executions.find(
            (x) => x.id === e.execution_id || x.project_id === e.project_id,
          );
          return (
            <article className="activity-row" key={`${e.id}-${i}`}>
              <div className="activity-meta">
                <time>{e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '—'}</time>
                {project === 'all' && execution && (
                  <span className="project-event-label">
                    {execution.project_name || execution.project_id.slice(0, 8)}
                  </span>
                )}
              </div>
              <div>
                {raw ? (
                  <details>
                    <summary>
                      {e.type} · {e.message?.slice(0, 100)}
                    </summary>
                    <pre className="activity-output">
                      {JSON.stringify(
                        events.find((original) => original.id === e.id) || e,
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                ) : (
                  <ActivityCard event={e} />
                )}
              </div>
            </article>
          );
        })}
        {!visible.length && <p className="muted">No activity yet.</p>}
      </div>
      {!following && (
        <button
          className="jump-latest"
          onClick={() => {
            setFollowing(true);
            viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: 'smooth' });
          }}
        >
          ↓ Jump to latest
        </button>
      )}
    </>
  );
}
