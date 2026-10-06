import { NumberInput } from './number-input';
import { useEffect, useState } from 'react';
import { mutate, useAction, useResource } from './api';
import { confirmDiscardChanges, UnsavedNotice, useUnsavedChanges } from './drafts';
import { RunBuilder } from './runs';
import type {
  Automation,
  AutomationOccurrence,
  Project,
  RunTemplate,
  UsageTrigger,
  UsageWindow,
} from './types';
import {
  ActionNotice,
  Badge,
  conditionLabel,
  date,
  duration,
  ErrorNotice,
  Link,
  Loading,
  PageTitle,
  UsageCards,
  useNavigation,
} from './ui';

const defaultTrigger: UsageTrigger = {
  window_minutes: 10080,
  limit_id: null,
  hours_before_reset: 12,
  min_remaining_percent: 1,
};

function triggerLabel(trigger: UsageTrigger) {
  return `${duration(trigger.window_minutes)} window resets within ${trigger.hours_before_reset}h · at least ${trigger.min_remaining_percent}% remaining${trigger.limit_id ? ` · ${trigger.limit_id}` : ''}`;
}

// Expiration is also reflected in an open page without waiting for another worker evaluation.
function useNow() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

function occurrenceStatus(item: AutomationOccurrence, now: number) {
  return ['pending', 'ready'].includes(item.status) && new Date(item.resets_at).getTime() <= now
    ? 'expired'
    : item.status;
}

export function AutomationsPage() {
  const rules = useResource<Automation[]>('/automations');
  const occurrences = useResource<AutomationOccurrence[]>('/automation-occurrences');
  const [editing, setEditing] = useState<Automation | 'new'>();
  const action = useAction();
  const now = useNow();
  const pending =
    occurrences.data?.filter((item) => occurrenceStatus(item, now) === 'pending') ?? [];
  const history =
    occurrences.data?.filter((item) => occurrenceStatus(item, now) !== 'pending') ?? [];
  return (
    <>
      <PageTitle
        title="Automations"
        description="Consume remaining usage before the weekly reset. Conditions are checked every 15 minutes."
        actions={
          <button
            className="primary"
            onClick={() => {
              if (confirmDiscardChanges()) setEditing('new');
            }}
          >
            New automation
          </button>
        }
      />
      <ErrorNotice error={rules.error || occurrences.error} />
      <ActionNotice {...action} />
      {editing ? (
        <AutomationEditor
          key={editing === 'new' ? 'new' : editing.id}
          automation={editing === 'new' ? undefined : editing}
          close={() => setEditing(undefined)}
        />
      ) : (
        <>
          <section className="panel">
            <h2>Pending approvals</h2>
            {!occurrences.data ? (
              <Loading />
            ) : !pending.length ? (
              <p className="muted">No runs awaiting your approval.</p>
            ) : (
              pending.map((item) => (
                <div className="row between automation-occurrence" key={item.id}>
                  <div>
                    <Link href={`/automation-occurrences/${item.id}`}>{item.automation_name}</Link>
                    <p className="small muted">Expires at reset: {date(item.resets_at)}</p>
                    <ErrorNotice error={item.delivery_error || item.last_error || undefined} />
                  </div>
                  <Link className="button" href={`/automation-occurrences/${item.id}`}>
                    Review run
                  </Link>
                </div>
              ))
            )}
          </section>
          <div className="schedule-list">
            {!rules.data ? (
              <Loading />
            ) : !rules.data.length ? (
              <section className="panel">
                <h2>No automations yet</h2>
                <p className="muted">
                  Choose a usage condition and save a run to launch automatically or after your
                  approval.
                </p>
              </section>
            ) : (
              rules.data.map((rule) => (
                <section className="panel" key={rule.id}>
                  <div className="row between wrap">
                    <div>
                      <h2>{rule.name}</h2>
                      <p className="small muted">{triggerLabel(rule.trigger)}</p>
                    </div>
                    <div className="row wrap">
                      <Badge status={rule.enabled ? 'enabled' : 'paused'} />
                      <button
                        disabled={action.busy}
                        onClick={() =>
                          void action.run(() =>
                            mutate(`/automations/${rule.id}`, 'PATCH', { enabled: !rule.enabled }),
                          )
                        }
                      >
                        {rule.enabled ? 'Pause' : 'Enable'}
                      </button>
                      <button onClick={() => setEditing(rule)}>Edit</button>
                      <button
                        disabled={action.busy}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete automation “${rule.name}”? Existing runs and history will remain.`,
                            )
                          )
                            void action.run(() => mutate(`/automations/${rule.id}`, 'DELETE'));
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  <p>
                    {rule.mode === 'automatic'
                      ? 'Launch automatically'
                      : 'Notify through ntfy and wait for approval'}{' '}
                    · {rule.run_template.projects.length}{' '}
                    {rule.run_template.projects.length === 1 ? 'project' : 'projects'}
                  </p>
                  <p className="small muted">
                    Once per matching reset window · Stops at reset · Last checked{' '}
                    {date(rule.last_checked_at)}
                  </p>
                  <div className="stop-chips">
                    {rule.run_template.stop_conditions.map((condition, i) => (
                      <span className="chip" key={i}>
                        {conditionLabel(condition)}
                      </span>
                    ))}
                  </div>
                  <ErrorNotice error={rule.last_error || undefined} />
                </section>
              ))
            )}
          </div>
          <section className="panel">
            <h2>History</h2>
            {!history.length ? (
              <p className="muted">
                Triggered runs and dismissed or expired requests will appear here.
              </p>
            ) : (
              history.map((item) => (
                <div className="row between automation-occurrence" key={item.id}>
                  <div>
                    <Link href={`/automation-occurrences/${item.id}`}>{item.automation_name}</Link>
                    <p className="small muted">
                      {item.limit_id} · Reset {date(item.resets_at)}
                    </p>
                    <ErrorNotice error={item.last_error || item.delivery_error || undefined} />
                  </div>
                  <div className="row">
                    <Badge status={occurrenceStatus(item, now)} />
                    {item.run_id && <Link href={`/runs/${item.run_id}`}>View run</Link>}
                  </div>
                </div>
              ))
            )}
          </section>
        </>
      )}
    </>
  );
}

function AutomationEditor({ automation, close }: { automation?: Automation; close: () => void }) {
  const [name, setName] = useState(automation?.name ?? 'Drain weekly usage');
  const [trigger, setTrigger] = useState(automation?.trigger ?? defaultTrigger);
  const [mode, setMode] = useState<Automation['mode']>(automation?.mode ?? 'approval');
  const usage = useResource<UsageWindow[]>('/usage');
  const ntfy = useResource<{ topic: string }>('/notifications/ntfy');
  const timing = useUnsavedChanges({ name, trigger, mode });
  const patch = (value: Partial<UsageTrigger>) => setTrigger((old) => ({ ...old, ...value }));
  const initial: RunTemplate = automation?.run_template ?? {
    projects: [],
    parallel: true,
    threshold_mode: 'graceful',
    stop_conditions: [
      { kind: 'usage', window_minutes: trigger.window_minutes, used_percent: 100 },
      { kind: 'provider_limit' },
      { kind: 'project_completed' },
    ],
  };
  return (
    <>
      <div className="row between">
        <h2>{automation ? `Edit ${automation.name}` : 'New automation'}</h2>
        <button
          onClick={() => {
            if (confirmDiscardChanges()) close();
          }}
        >
          Close editor
        </button>
      </div>
      <RunBuilder
        initial={initial}
        usageTarget={trigger}
        draftContext={{ name, trigger, mode }}
        submitLabel="Save automation"
        onSubmit={async (template, saved) => {
          await mutate(
            automation ? `/automations/${automation.id}` : '/automations',
            automation ? 'PATCH' : 'POST',
            {
              name,
              trigger,
              mode,
              enabled: automation?.enabled ?? true,
              run_template: template,
            },
          );
          saved();
          timing.markSaved();
          close();
        }}
      >
        <section className="panel">
          <h2>When to launch</h2>
          <label>
            Name
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <div className="form-grid">
            <label>
              Usage window (minutes)
              <NumberInput
                min={1}
                max={525600}
                required
                value={trigger.window_minutes}
                list="automation-window-durations"
                onValueChange={(value) => patch({ window_minutes: value })}
              />
            </label>
            <datalist id="automation-window-durations">
              <option value={10080}>Weekly</option>
              {[
                ...new Set(
                  usage.data?.map((w) => w.window_minutes).filter((v): v is number => !!v),
                ),
              ]
                .filter((v) => v !== 10080)
                .map((v) => (
                  <option key={v} value={v}>
                    {duration(v)}
                  </option>
                ))}
            </datalist>
            <label>
              Limit ID (optional)
              <input
                maxLength={200}
                value={trigger.limit_id || ''}
                placeholder="Any matching limit"
                onChange={(e) => patch({ limit_id: e.target.value || null })}
              />
            </label>
            <label>
              Reset within (hours)
              <NumberInput
                min={0.01}
                max={168}
                step="any"
                required
                value={trigger.hours_before_reset}
                onValueChange={(value) => patch({ hours_before_reset: value })}
              />
            </label>
            <label>
              Minimum remaining (%)
              <NumberInput
                min={0}
                max={100}
                step="any"
                required
                value={trigger.min_remaining_percent}
                onValueChange={(value) => patch({ min_remaining_percent: value })}
              />
            </label>
          </div>
          <fieldset>
            <legend>Launch mode</legend>
            <label className="check-row">
              <input
                type="radio"
                name="automation-mode"
                checked={mode === 'approval'}
                onChange={() => setMode('approval')}
              />
              <span>
                Notify and wait for approval
                <span className="hint">
                  Send an ntfy notification. You review and authorize the run on this website.
                </span>
              </span>
            </label>
            <label className="check-row">
              <input
                type="radio"
                name="automation-mode"
                checked={mode === 'automatic'}
                onChange={() => setMode('automatic')}
              />
              <span>
                Launch automatically
                <span className="hint">Start the saved run when the condition matches.</span>
              </span>
            </label>
          </fieldset>
          {mode === 'approval' && (
            <p className="small muted">
              {ntfy.data?.topic
                ? 'Uses your saved ntfy destination.'
                : 'Configure an ntfy topic before enabling approval mode.'}{' '}
              <Link href="/settings#notifications">Notification settings →</Link>
            </p>
          )}
          <p className="small muted">
            Checked every 15 minutes. One run per matching reset window. The run stops at reset,
            including during wrap-up, to avoid spending usage from the new window.
          </p>
          <ErrorNotice error={usage.error || ntfy.error} />
          <UnsavedNotice dirty={timing.dirty} />
        </section>
      </RunBuilder>
    </>
  );
}

export function AutomationApprovalPage({ id }: { id: string }) {
  const resource = useResource<AutomationOccurrence>(`/automation-occurrences/${id}`);
  const projects = useResource<Project[]>('/projects');
  const action = useAction();
  const { go } = useNavigation();
  const now = useNow();
  const item = resource.data;
  const status = item ? occurrenceStatus(item, now) : undefined;
  return (
    <>
      <PageTitle
        title={item?.automation_name ?? 'Automation request'}
        description="Review the saved run before authorizing it."
      />
      <Link href="/automations">← Automations</Link>
      <ErrorNotice error={resource.error || projects.error} />
      {!item ? (
        <Loading />
      ) : (
        <section className="panel">
          <div className="row between">
            <h2>Run configuration</h2>
            <Badge status={status!} />
          </div>
          <p>
            {item.limit_id} · {duration(item.window_minutes)} window · Expires at reset{' '}
            {date(item.resets_at)}
          </p>
          <p className="small muted">
            {(100 - item.matched_window.used_percent).toFixed(1)}% remained when triggered. Current
            observations are shown below; authorization refreshes usage and checks the rule again.
          </p>
          {item.current_usage && <UsageCards windows={item.current_usage} />}
          <dl className="metadata">
            {item.run_template.projects.map((config) => (
              <div key={config.project_id}>
                <dt>
                  {projects.data?.find((p) => p.id === config.project_id)?.name ||
                    config.project_id}
                </dt>
                <dd>
                  {config.model || 'Project default model'} · {config.reasoning_effort} reasoning
                </dd>
              </div>
            ))}
          </dl>
          <p>
            {item.run_template.parallel ? 'Parallel' : 'Sequential'} execution ·{' '}
            {item.run_template.threshold_mode === 'hard' ? 'Hard limit' : 'Graceful stop'}
          </p>
          <div className="stop-chips">
            {item.run_template.stop_conditions.map((condition, i) => (
              <span className="chip" key={i}>
                {conditionLabel(condition)}
              </span>
            ))}
          </div>
          <p className="small muted">
            Stops at reset with hard interruption. Only approved tasks are worked; the run may
            finish before exhausting usage.
          </p>
          <ErrorNotice error={item.last_error || item.delivery_error || undefined} />
          <ActionNotice {...action} />
          {status === 'pending' && (
            <div className="row wrap">
              <button
                className="primary"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    const result = await mutate<AutomationOccurrence>(
                      `/automation-occurrences/${id}/authorize`,
                      'POST',
                    );
                    if (result.run_id) go(`/runs/${result.run_id}`);
                    else resource.reload();
                  })
                }
              >
                Authorize run
              </button>
              <button
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await mutate(`/automation-occurrences/${id}/dismiss`, 'POST');
                    resource.reload();
                  })
                }
              >
                Dismiss
              </button>
            </div>
          )}
          {item.run_id && (
            <Link className="button" href={`/runs/${item.run_id}`}>
              View run
            </Link>
          )}
          {status === 'expired' && (
            <p className="muted">This request expired at reset. It cannot launch a run.</p>
          )}
        </section>
      )}
    </>
  );
}
