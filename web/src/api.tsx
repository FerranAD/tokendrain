import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { LiveEvent } from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('X-Tokendrain-Request', '1');
  if (options.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: 'same-origin',
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') message = body.detail;
      else if (body.detail && typeof body.detail === 'object' && 'message' in body.detail)
        message = String(body.detail.message);
      else if (Array.isArray(body.detail)) {
        message = body.detail
          .map(
            (v: { loc?: unknown[]; msg?: string }) =>
              `${v.loc?.slice(1).join('.') ?? ''}: ${v.msg ?? 'Invalid value'}`,
          )
          .join('; ');
      }
    } catch {
      /* Preserve the HTTP error when no JSON is supplied. */
    }
    if (response.status === 401) window.dispatchEvent(new Event('tokendrain:unauthorized'));
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function mutate<T>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: unknown,
) {
  return api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
}

interface EventState {
  revision: number;
  connected: boolean;
  events: LiveEvent[];
  refresh: () => void;
}

const Events = createContext<EventState>({
  revision: 0,
  connected: false,
  events: [],
  refresh: () => {},
});

export function EventsProvider({ children }: { children: ReactNode }) {
  const [revision, setRevision] = useState(0);
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const refresh = useCallback(() => setRevision((v) => v + 1), []);
  useEffect(() => {
    const stream = new EventSource('/api/v1/events', { withCredentials: true });
    let timer: ReturnType<typeof setTimeout> | undefined;
    stream.onopen = () => {
      setConnected(true);
      refresh();
    };
    stream.onerror = () => setConnected(false);
    stream.onmessage = (event: MessageEvent<string>) => {
      try {
        const data = JSON.parse(event.data) as LiveEvent;
        if (!data || typeof data.type !== 'string') return;
        setEvents((previous) => [
          ...previous.slice(-499),
          { ...data, id: data.id ?? event.lastEventId },
        ]);
        timer ??= setTimeout(() => {
          refresh();
          timer = undefined;
        }, 1500);
      } catch {
        /* A malformed event cannot prevent subsequent updates. */
      }
    };
    return () => {
      stream.close();
      clearTimeout(timer);
    };
  }, [refresh]);
  return (
    <Events.Provider value={{ revision, connected, events, refresh }}>{children}</Events.Provider>
  );
}

export const useEvents = () => useContext(Events);

export function useResource<T>(path: string | null) {
  const { revision } = useEvents();
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(!!path);
  const [localRevision, setLocalRevision] = useState(0);
  const lastPath = useRef(path);
  useEffect(() => {
    if (!path) {
      setData(undefined);
      setLoading(false);
      return;
    }
    if (lastPath.current !== path) {
      setData(undefined);
      lastPath.current = path;
    }
    const controller = new AbortController();
    setLoading(true);
    void api<T>(path, { signal: controller.signal })
      .then((value) => {
        setData(value);
        setError(undefined);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Request failed');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [path, revision, localRevision]);
  return { data, error, loading, reload: () => setLocalRevision((v) => v + 1) };
}

export function useAction() {
  const { refresh } = useEvents();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const lock = useRef(false);
  const run = async (operation: () => Promise<unknown>, success?: string): Promise<boolean> => {
    if (lock.current) return false;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await operation();
      refresh();
      setNotice(success);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Operation failed');
      return false;
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return {
    busy,
    error,
    notice,
    run,
    clear: () => {
      setError(undefined);
      setNotice(undefined);
    },
  };
}
