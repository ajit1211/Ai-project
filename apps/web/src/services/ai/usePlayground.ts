/**
 * The playground's state machine: the models, the draft request, one request in flight at
 * a time (streamed or not), the text as it arrives, and what the server reported about the
 * completion. The page only renders this and wires up inputs.
 */

import { useEffect, useRef, useState } from "react";

import { AiStreamError, chat, listModels, streamChat } from "../../api/ai";
import { ApiError } from "../../api/client";
import { describeError } from "../../components/forms";
import type { ChatMessage, ChatRequest, FinishReason, ModelInfo, Usage } from "../../types/ai";
import { retryHintFor } from "./errors";

export type PlaygroundStatus = "idle" | "streaming" | "done" | "error";

/** What is known about the current completion; fields fill in as events arrive. */
export interface PlaygroundResult {
  model: string | null;
  provider: string | null;
  providerModel: string | null;
  requestId: string | null;
  usage: Usage | null;
  /** Server-side latency, from `message.end` or the JSON response. */
  latencyMs: number | null;
  /** Measured in the browser: from send until the first content arrived. */
  ttftMs: number | null;
  attempts: number | null;
  fallbackUsed: boolean;
  finishReason: FinishReason | null;
  cancelled: boolean;
}

export interface PlaygroundError {
  code: string | null;
  message: string;
  /** What to do about it, when the code says a retry can help. */
  retryHint: string | null;
}

const emptyResult: PlaygroundResult = {
  model: null,
  provider: null,
  providerModel: null,
  requestId: null,
  usage: null,
  latencyMs: null,
  ttftMs: null,
  attempts: null,
  fallbackUsed: false,
  finishReason: null,
  cancelled: false,
};

export function toPlaygroundError(err: unknown): PlaygroundError {
  if (err instanceof ApiError) {
    return { code: err.code, message: describeError(err), retryHint: retryHintFor(err.code, err.retryAfter) };
  }
  if (err instanceof AiStreamError) {
    return {
      code: err.code,
      message: err.requestId ? `${err.message} (ref ${err.requestId})` : err.message,
      retryHint: err.retryable ? "This failure is temporary; try again." : null,
    };
  }
  return { code: null, message: describeError(err), retryHint: null };
}

function pickUsage(event: Usage): Usage {
  return {
    input_tokens: event.input_tokens,
    output_tokens: event.output_tokens,
    total_tokens: event.total_tokens,
    cached_input_tokens: event.cached_input_tokens,
    cache_write_input_tokens: event.cache_write_input_tokens,
    cost_usd: event.cost_usd,
  };
}

export function usePlayground() {
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("1024");
  const [stream, setStream] = useState(true);
  const [status, setStatus] = useState<PlaygroundStatus>("idle");
  const [text, setText] = useState("");
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const [error, setError] = useState<PlaygroundError | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const cancelled = { current: false };
    listModels()
      .then((list) => {
        if (!cancelled.current) setModels(list);
      })
      .catch((err: unknown) => {
        if (!cancelled.current) setModelsError(describeError(err));
      });
    return () => {
      cancelled.current = true;
    };
  }, []);

  // A request still running when the page unmounts is cancelled.
  useEffect(
    () => () => {
      controllerRef.current?.abort();
    },
    [],
  );

  const defaultModel = models?.find((m) => m.is_default) ?? models?.[0] ?? null;
  const model = models?.find((m) => m.name === selectedModel) ?? defaultModel;

  const send = async (): Promise<void> => {
    if (controllerRef.current || !model || !prompt.trim()) return;
    const controller = new AbortController();
    controllerRef.current = controller;

    const messages: ChatMessage[] = [];
    if (systemPrompt.trim()) messages.push({ role: "system", content: systemPrompt });
    messages.push({ role: "user", content: prompt });
    const request: ChatRequest = { model: model.name, messages, feature: "playground" };
    const tokens = Number.parseInt(maxOutputTokens, 10);
    if (Number.isInteger(tokens) && tokens > 0) request.max_output_tokens = tokens;

    setStatus("streaming");
    setText("");
    setError(null);
    setResult({ ...emptyResult, model: model.name });

    const startedAt = performance.now();
    const elapsed = () => Math.round(performance.now() - startedAt);
    let firstContentSeen = false;
    const noteFirstContent = () => {
      if (firstContentSeen) return;
      firstContentSeen = true;
      const ttftMs = elapsed();
      setResult((current) => ({ ...(current ?? emptyResult), ttftMs }));
    };

    try {
      if (stream) {
        for await (const event of streamChat(request, { signal: controller.signal })) {
          switch (event.type) {
            case "message.start":
              setResult((current) => ({
                ...(current ?? emptyResult),
                model: event.model,
                provider: event.provider,
                providerModel: event.provider_model,
                requestId: event.request_id,
              }));
              break;
            case "message.delta":
              noteFirstContent();
              setText((current) => current + event.text);
              break;
            case "usage": {
              const usage = pickUsage(event);
              setResult((current) => ({ ...(current ?? emptyResult), usage }));
              break;
            }
            case "message.end":
              setResult((current) => ({
                ...(current ?? emptyResult),
                finishReason: event.finish_reason,
                latencyMs: event.latency_ms,
                attempts: event.attempts,
                fallbackUsed: event.fallback_used,
              }));
              break;
            case "error":
              // streamChat throws for these; handled here so the switch stays exhaustive.
              throw new AiStreamError(event);
          }
        }
      } else {
        const response = await chat(request, { signal: controller.signal });
        noteFirstContent();
        setText(response.content);
        setResult((current) => ({
          ...(current ?? emptyResult),
          model: response.model,
          provider: response.provider,
          providerModel: response.provider_model,
          requestId: response.id,
          usage: response.usage,
          latencyMs: response.latency_ms,
          attempts: response.attempts,
          fallbackUsed: response.fallback_used,
          finishReason: response.finish_reason,
        }));
      }
      setStatus("done");
    } catch (err) {
      if (controller.signal.aborted) {
        setResult((current) => ({ ...(current ?? emptyResult), cancelled: true }));
        setStatus("done");
      } else {
        setError(toPlaygroundError(err));
        setStatus("error");
      }
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  };

  const cancel = (): void => {
    controllerRef.current?.abort();
  };

  return {
    models,
    modelsError,
    model,
    selectModel: setSelectedModel,
    prompt,
    setPrompt,
    systemPrompt,
    setSystemPrompt,
    maxOutputTokens,
    setMaxOutputTokens,
    stream,
    setStream,
    status,
    text,
    result,
    error,
    send,
    cancel,
  };
}

export type Playground = ReturnType<typeof usePlayground>;
