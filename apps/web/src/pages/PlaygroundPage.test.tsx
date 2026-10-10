import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chat, getMyUsage, listModels, streamChat, type RequestControl } from "../api/ai";
import { ApiError } from "../api/client";
import { AuthContext, type AuthContextValue } from "../auth/AuthContext";
import type { ChatRequest, ChatResponse, ModelInfo, StreamEvent, UsageSummary } from "../types/ai";
import { PlaygroundPage } from "./PlaygroundPage";

vi.mock("../api/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/ai")>();
  return { ...actual, listModels: vi.fn(), chat: vi.fn(), streamChat: vi.fn(), getMyUsage: vi.fn() };
});

const models: ModelInfo[] = [
  {
    name: "fast",
    display_name: "Fast",
    description: "Cheap and quick.",
    is_default: false,
    capabilities: ["chat", "streaming"],
    context_window: 128000,
    max_output_tokens: 4096,
  },
  {
    name: "balanced",
    display_name: "Balanced",
    description: "The everyday model.",
    is_default: true,
    capabilities: ["chat", "streaming", "structured_output"],
    context_window: 200000,
    max_output_tokens: 8192,
  },
];

const usageSummary: UsageSummary = {
  scope: "me",
  organization_id: "org_1",
  period: { days: 30, since: "2026-09-09T00:00:00Z" },
  totals: {
    requests: 12,
    errors: 1,
    input_tokens: 300,
    output_tokens: 156,
    total_tokens: 456,
    cost_usd: "0.1200",
    unpriced_requests: 0,
  },
  by_model: [],
  by_day: [],
  recent: [],
};

const streamed: StreamEvent[] = [
  {
    type: "message.start",
    id: "msg_1",
    model: "balanced",
    provider: "openai",
    provider_model: "gpt-4o-mini",
    request_id: "req_1",
  },
  { type: "message.delta", text: "Hello" },
  { type: "message.delta", text: " world" },
  {
    type: "usage",
    input_tokens: 12,
    output_tokens: 34,
    total_tokens: 46,
    cached_input_tokens: 0,
    cache_write_input_tokens: 0,
    cost_usd: "0.000120",
  },
  { type: "message.end", finish_reason: "stop", latency_ms: 420, attempts: 1, fallback_used: false },
];

const completion: ChatResponse = {
  id: "msg_2",
  model: "balanced",
  provider: "anthropic",
  provider_model: "claude-x",
  content: "Plain answer",
  parsed: null,
  finish_reason: "stop",
  usage: streamed[3] as ChatResponse["usage"],
  latency_ms: 300,
  attempts: 2,
  fallback_used: true,
};

function streamOf(events: StreamEvent[]) {
  return async function* (): AsyncGenerator<StreamEvent, void, undefined> {
    for (const event of events) {
      await Promise.resolve();
      yield event;
    }
  };
}

const noop = () => Promise.resolve();
const auth: AuthContextValue = {
  status: "authenticated",
  user: null,
  organizations: [],
  activeOrganizationId: null,
  login: noop,
  logout: noop,
  switchOrganization: noop,
  reloadUser: noop,
  reloadOrganizations: () => Promise.resolve([]),
};

function renderPage() {
  return render(
    <AuthContext.Provider value={auth}>
      <MemoryRouter initialEntries={["/playground"]}>
        <PlaygroundPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

/** The value next to a label in the response footer (or another region's stats). */
function statValue(label: string, region = "Response"): string | undefined {
  const scope = within(screen.getByRole("region", { name: region }));
  return scope.getByText(label).nextElementSibling?.textContent ?? undefined;
}

async function typePromptAndGenerate(prompt: string): Promise<void> {
  await screen.findByRole("option", { name: "Balanced (default)" });
  fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: prompt } });
  fireEvent.click(screen.getByRole("button", { name: "Generate" }));
}

describe("PlaygroundPage", () => {
  beforeEach(() => {
    vi.mocked(listModels).mockResolvedValue(models);
    vi.mocked(getMyUsage).mockResolvedValue(usageSummary);
    vi.mocked(streamChat).mockImplementation(streamOf(streamed));
    vi.mocked(chat).mockResolvedValue(completion);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("lists the models and preselects the default one", async () => {
    renderPage();
    await screen.findByRole("option", { name: "Balanced (default)" });
    expect(screen.getByRole("option", { name: "Fast" })).toBeTruthy();
    expect(screen.getByLabelText<HTMLSelectElement>("Model").value).toBe("balanced");
    expect(screen.getByText(/The everyday model\./)).toBeTruthy();
    expect(screen.getByLabelText<HTMLInputElement>("Stream").checked).toBe(true);
  });

  it("streams a completion and shows the text and its usage", async () => {
    renderPage();
    await typePromptAndGenerate("Say hi");

    await screen.findByText("Hello world");
    await screen.findByText("done");

    expect(vi.mocked(streamChat)).toHaveBeenCalledWith(
      {
        model: "balanced",
        messages: [{ role: "user", content: "Say hi" }],
        feature: "playground",
        max_output_tokens: 1024,
      } satisfies ChatRequest,
      { signal: expect.any(AbortSignal) as AbortSignal } satisfies RequestControl,
    );
    expect(statValue("Tokens")).toBe("12 in · 34 out · 46 total");
    expect(statValue("Cost")).toBe("$0.000120");
    expect(statValue("Latency")).toBe("420 ms");
    expect(statValue("First token")).toMatch(/^\d+ ms$/);
    expect(statValue("Served by")).toBe("openai/gpt-4o-mini");
    expect(statValue("Finish reason")).toBe("stop");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Generate" }).disabled).toBe(false);
  });

  it("sends the system prompt and the chosen model", async () => {
    renderPage();
    await screen.findByRole("option", { name: "Balanced (default)" });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "fast" } });
    fireEvent.change(screen.getByLabelText("System prompt"), { target: { value: "Be terse." } });
    fireEvent.change(screen.getByLabelText("Max output tokens"), { target: { value: "64" } });
    await typePromptAndGenerate("Say hi");
    await screen.findByText("Hello world");

    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toEqual({
      model: "fast",
      messages: [
        { role: "system", content: "Be terse." },
        { role: "user", content: "Say hi" },
      ],
      feature: "playground",
      max_output_tokens: 64,
    });
  });

  it("uses the JSON endpoint when streaming is off and flags a fallback", async () => {
    renderPage();
    await screen.findByRole("option", { name: "Balanced (default)" });
    fireEvent.click(screen.getByLabelText("Stream"));
    await typePromptAndGenerate("Say hi");

    await screen.findByText("Plain answer");
    expect(vi.mocked(chat)).toHaveBeenCalledOnce();
    expect(vi.mocked(streamChat)).not.toHaveBeenCalled();
    expect(screen.getByText("fallback used")).toBeTruthy();
    expect(statValue("Served by")).toContain("anthropic/claude-x");
    expect(statValue("Finish reason")).toContain("2 attempts");
  });

  it("shows the usage card and refreshes it after a completion", async () => {
    renderPage();
    expect(await screen.findByText("Requests")).toBeTruthy();
    expect(statValue("Requests", "Your usage (last 30 days)")).toBe("12");
    expect(statValue("Total tokens", "Your usage (last 30 days)")).toBe("456");
    expect(screen.getByText("$0.1200")).toBeTruthy();
    expect(vi.mocked(getMyUsage)).toHaveBeenCalledWith(30);
    expect(vi.mocked(getMyUsage)).toHaveBeenCalledTimes(1);

    await typePromptAndGenerate("Say hi");
    await screen.findByText("done");
    expect(vi.mocked(getMyUsage)).toHaveBeenCalledTimes(2);
  });

  it("explains a refusal and the retry hint for an exhausted quota", async () => {
    vi.mocked(streamChat).mockImplementation(async function* () {
      await Promise.resolve();
      throw new ApiError(
        429,
        { code: "AI_QUOTA_EXCEEDED", message: "Quota exhausted." },
        "req_7",
        30,
      );
      yield* []; // never reached; keeps the function a generator for require-yield
    });
    renderPage();
    await typePromptAndGenerate("Say hi");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Quota exhausted. (ref req_7) Try again in 30 s.");
    expect(screen.getByText("error")).toBeTruthy();
  });

  it("cancels a running stream", async () => {
    vi.mocked(streamChat).mockImplementation(async function* (_request, control?: RequestControl) {
      yield { type: "message.delta", text: "Hel" };
      await new Promise<never>((_, reject) => {
        control?.signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    });
    renderPage();
    await typePromptAndGenerate("Say hi");

    await screen.findByText("Hel");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Generate" }).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(await screen.findAllByText("cancelled")).not.toHaveLength(0);
    expect(screen.getByText("Hel")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Generate" }).disabled).toBe(false);
  });
});
