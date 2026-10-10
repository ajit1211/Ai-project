/**
 * The conversation's model, from `GET /ai/models/`. A conversation without a model of its own
 * ("") uses the organization's default, so that one is shown selected; picking another
 * changes the conversation (the page PATCHes it).
 */

import { useId } from "react";

import type { ModelInfo } from "../../types/ai";

export interface ModelSelectorProps {
  models: ModelInfo[] | null;
  /** Why the models could not be loaded. */
  error: string | null;
  /** The conversation's model; "" means the organization's default. */
  value: string;
  onChange: (model: string) => void;
  disabled?: boolean;
}

export function ModelSelector({ models, error, value, onChange, disabled = false }: ModelSelectorProps) {
  const id = useId();
  const fallback = models?.find((model) => model.is_default)?.name ?? models?.[0]?.name ?? "";
  const selected = value || fallback;
  const known = models?.some((model) => model.name === selected) ?? false;

  let placeholder: string | null = null;
  if (!models) placeholder = error ? "Models unavailable" : "Loading models…";
  else if (selected && !known) placeholder = `${selected} (unavailable)`;

  return (
    <div className="model-selector">
      <label htmlFor={id} className="visually-hidden">
        Model
      </label>
      <select
        id={id}
        value={selected}
        title={error ?? undefined}
        disabled={disabled || !models || models.length === 0}
        onChange={(event) => {
          if (event.target.value !== selected) onChange(event.target.value);
        }}
      >
        {placeholder !== null && <option value={selected}>{placeholder}</option>}
        {models?.map((model) => (
          <option key={model.name} value={model.name}>
            {model.display_name}
            {model.is_default ? " (default)" : ""}
          </option>
        ))}
      </select>
    </div>
  );
}
