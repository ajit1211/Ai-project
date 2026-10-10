/**
 * The AI playground: a development surface for the AI Gateway. Pick a model, send a prompt,
 * watch the stream, read tokens, cost and latency. The Phase 05 chat UI replaces it.
 */

import { useEffect, useState } from "react";

import { getMyUsage } from "../api/ai";
import { Alert, Field, describeError } from "../components/forms";
import {
  usePlayground,
  type PlaygroundResult,
  type PlaygroundStatus,
} from "../services/ai/usePlayground";
import type { UsageSummary } from "../types/ai";

const USAGE_DAYS = 30;

const count = (value: number): string => value.toLocaleString();
const millis = (value: number | null): string => (value === null ? "—" : `${count(value)} ms`);
const cost = (value: string | null): string => (value === null ? "n/a" : `$${value}`);

function StatusBadge({ status, cancelled }: { status: PlaygroundStatus; cancelled: boolean }) {
  if (status === "idle") return null;
  if (status === "streaming") return <span className="badge badge-waiting">streaming…</span>;
  if (status === "error") return <span className="badge badge-down">error</span>;
  if (cancelled) return <span className="badge badge-waiting">cancelled</span>;
  return <span className="badge badge-up">done</span>;
}

function ResponseFooter({ result }: { result: PlaygroundResult }) {
  const { usage } = result;
  return (
    <dl className="stats">
      <div>
        <dt>Tokens</dt>
        <dd>
          {usage
            ? `${count(usage.input_tokens)} in · ${count(usage.output_tokens)} out · ${count(usage.total_tokens)} total`
            : "—"}
        </dd>
      </div>
      <div>
        <dt>Cost</dt>
        <dd>{usage ? cost(usage.cost_usd) : "—"}</dd>
      </div>
      <div>
        <dt>Latency</dt>
        <dd>{millis(result.latencyMs)}</dd>
      </div>
      <div>
        <dt>First token</dt>
        <dd>{millis(result.ttftMs)}</dd>
      </div>
      <div>
        <dt>Served by</dt>
        <dd>
          {result.provider ? `${result.provider}/${result.providerModel ?? ""}` : "—"}
          {result.fallbackUsed && <span className="badge badge-waiting">fallback used</span>}
        </dd>
      </div>
      <div>
        <dt>Finish reason</dt>
        <dd>
          {result.cancelled ? "cancelled" : (result.finishReason ?? "—")}
          {result.attempts !== null && result.attempts > 1 && (
            <span className="muted"> · {String(result.attempts)} attempts</span>
          )}
        </dd>
      </div>
    </dl>
  );
}

function UsageCard({ status }: { status: PlaygroundStatus }) {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Loaded on mount and again after each completed request, so the numbers stay honest.
  useEffect(() => {
    if (status === "streaming") return;
    const cancelled = { current: false };
    getMyUsage(USAGE_DAYS)
      .then((summary) => {
        if (cancelled.current) return;
        setUsage(summary);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled.current) setError(describeError(err));
      });
    return () => {
      cancelled.current = true;
    };
  }, [status]);

  return (
    <section className="card section" aria-labelledby="playground-usage-heading">
      <h2 id="playground-usage-heading">Your usage (last {String(USAGE_DAYS)} days)</h2>
      {error && <Alert kind="error">{error}</Alert>}
      {usage ? (
        <dl className="stats">
          <div>
            <dt>Requests</dt>
            <dd>{count(usage.totals.requests)}</dd>
          </div>
          <div>
            <dt>Total tokens</dt>
            <dd>{count(usage.totals.total_tokens)}</dd>
          </div>
          <div>
            <dt>Cost</dt>
            <dd>
              {cost(usage.totals.cost_usd)}
              {usage.totals.unpriced_requests > 0 && (
                <span className="muted"> · {count(usage.totals.unpriced_requests)} unpriced</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Errors</dt>
            <dd>{count(usage.totals.errors)}</dd>
          </div>
        </dl>
      ) : (
        !error && <p className="muted">Loading…</p>
      )}
    </section>
  );
}

export function PlaygroundPage() {
  const playground = usePlayground();
  const { models, model, status, result, error } = playground;
  const busy = status === "streaming";

  return (
    <>
      <h1>NEXA AI Playground</h1>
      <p className="subtitle">
        A development surface for the AI Gateway: pick a model, send a prompt, watch the stream.
      </p>
      {playground.modelsError && <Alert kind="error">{playground.modelsError}</Alert>}

      <div className="playground">
        <form
          className="card section"
          onSubmit={(event) => {
            event.preventDefault();
            void playground.send();
          }}
        >
          <Field label="Model">
            <select
              value={model?.name ?? ""}
              disabled={!models || busy}
              onChange={(e) => {
                playground.selectModel(e.target.value);
              }}
            >
              {models ? (
                models.map((m) => (
                  <option key={m.name} value={m.name}>
                    {m.display_name}
                    {m.is_default ? " (default)" : ""}
                  </option>
                ))
              ) : (
                <option value="">Loading models…</option>
              )}
            </select>
          </Field>
          {model && (
            <p className="muted small">
              {model.description} · context {count(model.context_window)} · max output{" "}
              {count(model.max_output_tokens)} · {model.capabilities.join(", ")}
            </p>
          )}

          <details className="disclosure">
            <summary>System prompt (optional)</summary>
            <Field label="System prompt">
              <textarea
                rows={3}
                value={playground.systemPrompt}
                disabled={busy}
                onChange={(e) => {
                  playground.setSystemPrompt(e.target.value);
                }}
              />
            </Field>
          </details>

          <Field label="Prompt">
            <textarea
              rows={6}
              required
              value={playground.prompt}
              disabled={busy}
              placeholder="Ask the model something…"
              onChange={(e) => {
                playground.setPrompt(e.target.value);
              }}
            />
          </Field>

          <div className="inline-fields">
            <Field label="Max output tokens">
              <input
                type="number"
                min={1}
                max={model?.max_output_tokens}
                step={1}
                value={playground.maxOutputTokens}
                disabled={busy}
                onChange={(e) => {
                  playground.setMaxOutputTokens(e.target.value);
                }}
              />
            </Field>
            <label className="check">
              <input
                type="checkbox"
                checked={playground.stream}
                disabled={busy}
                onChange={(e) => {
                  playground.setStream(e.target.checked);
                }}
              />
              Stream
            </label>
          </div>

          <div className="actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busy || !model || !playground.prompt.trim()}
            >
              Generate
            </button>
            {busy && (
              <button
                type="button"
                className="btn"
                onClick={() => {
                  playground.cancel();
                }}
              >
                Cancel
              </button>
            )}
          </div>
        </form>

        <section className="card section" aria-label="Response">
          <h2>
            Response <StatusBadge status={status} cancelled={result?.cancelled ?? false} />
          </h2>
          {error && (
            <Alert kind="error">
              {error.message}
              {error.retryHint && ` ${error.retryHint}`}
            </Alert>
          )}
          {playground.text ? (
            <pre className="response">{playground.text}</pre>
          ) : (
            <p className="response muted">
              {busy ? "Waiting for the first token…" : "The response appears here."}
            </p>
          )}
          {result && <ResponseFooter result={result} />}
        </section>
      </div>

      <UsageCard status={status} />
    </>
  );
}
