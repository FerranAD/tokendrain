import { useEffect, useState } from 'react';
import { mutate, useAction, useResource } from './api';
import type { AgentStatus } from './types';
import { ActionNotice, ErrorNotice, Loading } from './ui';

export function AgentSettings({ agent }: { agent: AgentStatus }) {
  const action = useAction();
  return (
    <section className="panel" id="agent">
      <h2>Agent</h2>
      <label>
        Use for all projects and runs
        <select
          aria-label="Agent"
          value={agent.name}
          disabled={action.busy}
          onChange={(event) =>
            void action.run(
              () => mutate('/agent', 'PUT', { name: event.target.value }),
              'Agent changed.',
            )
          }
        >
          <option value="codex">Codex</option>
          <option value="claude_code">Claude Code</option>
        </select>
      </label>
      <p className="small muted">
        Reminders, schedules, and automations follow this choice. Finish active runs before
        switching.
      </p>
      <ActionNotice {...action} />
    </section>
  );
}

interface ClaudeStatus {
  connected: boolean;
  account_label?: string | null;
  credential_error?: string | null;
}
interface Login {
  id: string;
  status: 'starting' | 'waiting' | 'connected' | 'failed' | 'expired' | 'cancelled';
  authorization_url?: string | null;
  error?: string | null;
}

export function ClaudeSettings() {
  const account = useResource<ClaudeStatus>('/auth/claude');
  const [login, setLogin] = useState<Login | null>(null);
  const [code, setCode] = useState('');
  const action = useAction();
  useEffect(() => {
    if (!login || !['starting', 'waiting'].includes(login.status)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const response = await fetch(`/api/v1/auth/claude/login/${login.id}`, {
          credentials: 'same-origin',
        });
        if (!response.ok) throw new Error('Could not check Claude login. Try connecting again.');
        const result = (await response.json()) as Login;
        if (cancelled) return;
        setLogin(result);
        if (result.status === 'connected') account.reload();
      } catch {
        if (!cancelled)
          setLogin({
            ...login,
            status: 'failed',
            error: 'Could not check Claude login. Try connecting again.',
          });
      }
    };
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [login?.id, login?.status]);
  const pending = !!login && ['starting', 'waiting'].includes(login.status);
  return (
    <section className="panel" id="claude">
      <h2>Claude Code account</h2>
      <p className="muted">
        Connect your Claude subscription. Sign in through Claude, then return here.
      </p>
      <ErrorNotice
        error={account.error || account.data?.credential_error || login?.error || undefined}
      />
      {!account.data ? (
        <Loading />
      ) : (
        <>
          <p>
            {account.data.connected
              ? account.data.account_label || 'Claude connected'
              : 'Not connected'}
          </p>
          {!pending && (
            <div className="row wrap">
              <button
                onClick={() =>
                  void action.run(async () => {
                    setCode('');
                    setLogin(await mutate<Login>('/auth/claude/login', 'POST'));
                  })
                }
                disabled={action.busy}
              >
                {account.data.connected ? 'Reconnect Claude Code' : 'Connect Claude Code'}
              </button>
              {account.data.connected && (
                <>
                  <button
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(async () => {
                        await mutate('/auth/claude/check', 'POST');
                        account.reload();
                      }, 'Claude connection checked.')
                    }
                  >
                    Check connection
                  </button>
                  <button
                    className="danger"
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(async () => {
                        await mutate('/auth/claude', 'DELETE');
                        setLogin(null);
                        account.reload();
                      }, 'Claude disconnected.')
                    }
                  >
                    Disconnect
                  </button>
                </>
              )}
            </div>
          )}
          {pending && (
            <div className="callout">
              {login.authorization_url ? (
                <a
                  className="button"
                  href={login.authorization_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Sign in to Claude ↗
                </a>
              ) : (
                <p>Starting Claude login…</p>
              )}
              <p className="small">
                If Claude shows a login code, paste it below. Otherwise this page updates when login
                completes.
              </p>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void action.run(async () => {
                    await mutate(`/auth/claude/login/${login.id}/code`, 'POST', { code });
                    setCode('');
                  });
                }}
              >
                <label>
                  Claude login code
                  <input
                    autoComplete="off"
                    required
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                  />
                </label>
                <div className="row wrap">
                  <button disabled={action.busy || login.status !== 'waiting'}>
                    Complete login
                  </button>
                  <button
                    type="button"
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(async () => {
                        await mutate(`/auth/claude/login/${login.id}`, 'DELETE');
                        setLogin(null);
                        setCode('');
                      })
                    }
                  >
                    Cancel login
                  </button>
                </div>
              </form>
            </div>
          )}
          {login?.status === 'connected' && <p className="positive">Claude Code connected.</p>}
        </>
      )}
      <ActionNotice {...action} />
    </section>
  );
}
