import { NumberInput } from './number-input';
import { useState } from 'react';
import { mutate, useAction, useResource } from './api';
import { UnsavedNotice, useUnsavedChanges } from './drafts';
import { ActionNotice, ErrorNotice, Loading } from './ui';

interface UsageAlert {
  id: string;
  enabled: boolean;
  window_minutes: number;
  limit_id: string | null;
  hours_before_reset: number;
  min_remaining_percent: number;
}

interface NtfyConfig {
  enabled: boolean;
  server_url: string;
  topic: string;
  rules: UsageAlert[];
}

interface NtfyStatus extends NtfyConfig {
  has_token: boolean;
  delivery: {
    last_sent_at?: string;
    last_checked_at?: string;
    last_error?: string | null;
  };
}

function defaultReminder(windowMinutes = 10080): UsageAlert {
  return {
    id: Array.from(crypto.getRandomValues(new Uint32Array(4)), (value) => value.toString(16)).join(
      '-',
    ),
    enabled: true,
    window_minutes: windowMinutes,
    limit_id: null,
    hours_before_reset: windowMinutes === 300 ? 1 : 12,
    min_remaining_percent: 10,
  };
}

export function NotificationSettings() {
  const resource = useResource<NtfyStatus>('/notifications/ntfy');
  return (
    <section className="panel" id="notifications">
      <h2>Notifications</h2>
      <p className="muted">
        Get an ntfy reminder when your Codex usage limit is about to reset and you still have
        capacity left. Subscribe to the same server and topic in your ntfy app.
      </p>
      <ErrorNotice error={resource.error} />
      {resource.data ? (
        <NotificationForm initial={resource.data} reload={resource.reload} />
      ) : resource.loading ? (
        <Loading />
      ) : null}
    </section>
  );
}

function NotificationForm({ initial, reload }: { initial: NtfyStatus; reload: () => void }) {
  const [config, setConfig] = useState<NtfyConfig>(() => ({
    enabled: initial.enabled,
    server_url: initial.server_url,
    topic: initial.topic,
    rules: initial.rules.length ? initial.rules : [defaultReminder()],
  }));
  const [token, setToken] = useState('');
  const [clearToken, setClearToken] = useState(false);
  const draft = useUnsavedChanges({ config, token, clearToken });
  const action = useAction();
  const updateRule = (id: string, patch: Partial<UsageAlert>) =>
    setConfig((value) => ({
      ...value,
      rules: value.rules.map((rule) => (rule.id === id ? { ...rule, ...patch } : rule)),
    }));
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void action.run(async () => {
          await mutate('/notifications/ntfy', 'PUT', {
            ...config,
            clear_token: clearToken,
            ...(token ? { access_token: token } : {}),
          });
          setToken('');
          setClearToken(false);
          draft.markSaved({ config, token: '', clearToken: false });
          reload();
        }, 'Notification settings saved.');
      }}
    >
      <fieldset className="notification-controls" disabled={action.busy}>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={config.enabled}
            onChange={(event) => setConfig({ ...config, enabled: event.target.checked })}
          />
          Enable usage reminders
        </label>
        <div className="form-grid">
          <label>
            ntfy server URL
            <input
              type="url"
              required
              value={config.server_url}
              onChange={(event) => setConfig({ ...config, server_url: event.target.value })}
              placeholder="https://ntfy.sh"
            />
          </label>
          <label>
            Topic
            <input
              required={config.enabled}
              pattern="[a-zA-Z0-9_\-]+"
              maxLength={200}
              value={config.topic}
              onChange={(event) => setConfig({ ...config, topic: event.target.value })}
              placeholder="your-private-topic"
            />
          </label>
        </div>
        <p className="small muted">
          Use a private or hard-to-guess topic. Public topics can be read by other subscribers.
        </p>
        <label>
          {initial.has_token ? 'Replace access token (optional)' : 'Access token (optional)'}
          <input
            type="password"
            autoComplete="new-password"
            value={token}
            disabled={clearToken}
            onChange={(event) => setToken(event.target.value)}
            placeholder={initial.has_token ? 'Leave blank to keep the saved token' : 'tk_…'}
          />
        </label>
        {initial.has_token && (
          <label className="checkbox">
            <input
              type="checkbox"
              checked={clearToken}
              onChange={(event) => {
                setClearToken(event.target.checked);
                setToken('');
              }}
            />
            Remove saved access token
          </label>
        )}
        <h3>Usage reminder rules</h3>
        <p className="small muted">
          Get a reminder to use what's left before reset. Start with the weekly reminder below, or
          add one for the 5-hour window. Checked every 15 minutes; sent once per reset.
        </p>
        {config.rules.map((rule, index) => (
          <div className="inset top-space" key={rule.id}>
            <div className="row between">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={rule.enabled}
                  onChange={(event) => updateRule(rule.id, { enabled: event.target.checked })}
                />
                Reminder {index + 1}
              </label>
              <button
                type="button"
                className="quiet danger"
                onClick={() =>
                  setConfig({
                    ...config,
                    rules: config.rules.filter((item) => item.id !== rule.id),
                  })
                }
              >
                Remove reminder {index + 1}
              </button>
            </div>
            <div className="form-grid">
              <label>
                Usage window
                <select
                  aria-label="Usage window"
                  value={rule.window_minutes}
                  onChange={(event) =>
                    updateRule(rule.id, {
                      window_minutes: Number(event.target.value),
                      hours_before_reset: event.target.value === '300' ? 1 : 12,
                      limit_id: null,
                    })
                  }
                >
                  <option value={300}>5-hour window</option>
                  <option value={10080}>Weekly window</option>
                </select>
              </label>
              <label>
                Reset within (hours)
                <NumberInput
                  min={0.1}
                  max={rule.window_minutes === 300 ? 5 : 168}
                  step={0.1}
                  required
                  value={rule.hours_before_reset}
                  onValueChange={(value) => updateRule(rule.id, { hours_before_reset: value })}
                />
              </label>
              <label>
                Minimum remaining usage (%)
                <NumberInput
                  min={0}
                  max={100}
                  step={0.1}
                  required
                  value={rule.min_remaining_percent}
                  onValueChange={(value) => updateRule(rule.id, { min_remaining_percent: value })}
                />
              </label>
            </div>
            <p className="small muted">
              Remind me when the {rule.window_minutes === 300 ? '5-hour' : 'weekly'} window resets
              within {rule.hours_before_reset} {rule.hours_before_reset === 1 ? 'hour' : 'hours'}{' '}
              and at least {rule.min_remaining_percent}% of usage remains.
            </p>
          </div>
        ))}
        <button
          type="button"
          className="top-space"
          disabled={config.rules.length >= 20}
          onClick={() =>
            setConfig({
              ...config,
              rules: [
                ...config.rules,
                defaultReminder(
                  config.rules.some((rule) => rule.window_minutes === 10080) ? 300 : 10080,
                ),
              ],
            })
          }
        >
          Add usage reminder
        </button>
        <UnsavedNotice dirty={draft.dirty} />
        <div className="row wrap top-space">
          <button>Save notifications</button>
          <button
            type="button"
            disabled={draft.dirty || !initial.topic}
            onClick={() =>
              void action.run(
                () => mutate('/notifications/ntfy/test', 'POST'),
                'Test notification sent.',
              )
            }
          >
            Send test notification
          </button>
        </div>
      </fieldset>
      {initial.delivery.last_sent_at && (
        <p className="small muted">
          Last reminder sent {new Date(initial.delivery.last_sent_at).toLocaleString()}
        </p>
      )}
      <ErrorNotice error={initial.delivery.last_error || undefined} />
      <ActionNotice {...action} />
    </form>
  );
}
