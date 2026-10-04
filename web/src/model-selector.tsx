import { useEffect } from 'react';
import { useResource } from './api';
import type { CodexModel } from './types';

export function ModelSelector({
  model,
  effort,
  onChange,
  defaults = false,
}: {
  defaults?: boolean;
  model: string;
  effort: string;
  onChange: (values: { model: string; reasoning_effort: string }) => void;
}) {
  const models = useResource<CodexModel[]>('/auth/openai/models');
  const selected =
    models.data?.find((m) => m.id === model) ??
    (!model ? models.data?.find((m) => m.is_default) : undefined);
  const efforts = selected?.reasoning_efforts?.length
    ? selected.reasoning_efforts
    : ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  useEffect(() => {
    const supported = selected?.reasoning_efforts;
    if (supported?.length && !supported.includes(effort)) {
      onChange({ model, reasoning_effort: supported.includes('medium') ? 'medium' : supported[0] });
    }
  }, [selected, model, effort, onChange]);
  return (
    <div className="form-grid">
      <label>
        {defaults ? 'Default model' : 'Model'}
        {models.data?.length ? (
          <select
            aria-label={defaults ? 'Default model' : 'Model'}
            value={model}
            onChange={(e) => {
              const chosen = models.data?.find((m) => m.id === e.target.value);
              const supported = chosen?.reasoning_efforts;
              onChange({
                model: e.target.value,
                reasoning_effort:
                  supported?.length && !supported.includes(effort)
                    ? supported.includes('medium')
                      ? 'medium'
                      : supported[0]
                    : effort,
              });
            }}
          >
            <option value="">Provider default</option>
            {model && !models.data.some((m) => m.id === model) && (
              <option value={model}>{model}</option>
            )}
            {models.data.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name || m.id}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={model}
            onChange={(e) => onChange({ model: e.target.value, reasoning_effort: effort })}
            placeholder="Provider default or model ID"
          />
        )}
      </label>
      <label>
        {defaults ? 'Default reasoning effort' : 'Reasoning'}
        <select
          aria-label={defaults ? 'Default reasoning effort' : 'Reasoning'}
          value={effort}
          onChange={(e) => onChange({ model, reasoning_effort: e.target.value })}
        >
          {!efforts.includes(effort) && (
            <option value={effort}>{effort || 'Default (medium)'}</option>
          )}
          {efforts.map((e) => (
            <option key={e}>{e}</option>
          ))}
        </select>
      </label>
      {models.error && (
        <span className="hint">
          Model discovery unavailable. Enter a model ID or use the provider default.
        </span>
      )}
    </div>
  );
}
