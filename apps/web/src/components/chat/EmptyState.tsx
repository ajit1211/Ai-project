/** What the chat panel shows with no conversation open, or an open one without messages. */

export const EXAMPLE_PROMPTS = [
  "Explain how Django middleware works, with a short example.",
  "Write a Python function that retries an HTTP request with exponential backoff.",
  "Compare REST and GraphQL in a table.",
  "Draft a friendly message asking a teammate to review my pull request.",
] as const;

export interface EmptyStateProps {
  variant: "no-conversation" | "no-messages";
  /** Puts the example into the composer, to send or edit. */
  onPrompt: (prompt: string) => void;
}

export function EmptyState({ variant, onPrompt }: EmptyStateProps) {
  const fresh = variant === "no-messages";
  return (
    <div className="chat-empty">
      <div className="chat-empty-inner">
        <h2>{fresh ? "Ask anything" : "Start a conversation"}</h2>
        <p className="muted">
          {fresh
            ? "This conversation has no messages yet. Write one below, or start from an example."
            : "Pick a conversation, start a new one, or simply write a message below."}
        </p>
        <ul className="example-prompts" aria-label="Example prompts">
          {EXAMPLE_PROMPTS.map((prompt) => (
            <li key={prompt}>
              <button
                type="button"
                className="example-prompt"
                onClick={() => {
                  onPrompt(prompt);
                }}
              >
                {prompt}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
