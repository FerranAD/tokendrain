import { useEffect, useRef, useState } from 'react';

const drafts = new Set<symbol>();
export function confirmDiscardChanges() {
  return drafts.size === 0 || window.confirm('You have unsaved changes. Leave and discard them?');
}

export function useUnsavedChanges(value: unknown, initial: unknown = value) {
  const key = useRef(Symbol('draft'));
  const serialized = JSON.stringify(value);
  const latest = useRef(serialized);
  latest.current = serialized;
  const [saved, setSaved] = useState(() => JSON.stringify(initial));
  const dirty = serialized !== saved;
  useEffect(() => {
    const id = key.current;
    if (dirty) drafts.add(id);
    else drafts.delete(id);
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      drafts.delete(id);
      window.removeEventListener('beforeunload', beforeUnload);
    };
  }, [dirty, serialized, saved]);
  return {
    dirty,
    markSaved: (next: unknown = value) => {
      const checkpoint = JSON.stringify(next);
      if (checkpoint === latest.current) drafts.delete(key.current);
      else drafts.add(key.current);
      setSaved(checkpoint);
    },
    discard: () => !dirty || window.confirm('Discard your unsaved changes?'),
  };
}

export function UnsavedNotice({ dirty }: { dirty: boolean }) {
  return dirty ? (
    <div className="unsaved-notice" role="status">
      <span className="unsaved-dot" />
      <span>Unsaved changes</span>
      <span className="muted">Save before leaving this section.</span>
    </div>
  ) : null;
}
