/**
 * One task's page state: the task and its steps, followed live until the task is over, the
 * controls (pause, resume, cancel), and the decision on the action it waits for.
 *
 * Following: the task and its steps are loaded first. While the task is not over, its
 * progress stream delivers the changes: a step is added or replaced by its id, the task is
 * replaced (never by an older version of itself). A stream that closes after serving a while
 * (the server ends each one after an hour) is opened again. Should it fail, the task and its
 * steps are fetched every POLL_INTERVAL_MS instead (rounds are skipped while the tab is
 * hidden), until the task is over. Everything stops when the page goes away.
 *
 * After a control or a decision the task is fetched again, whatever the answer, so the page
 * shows it as it is now; a refused decision (the action changed, expired or was already
 * decided) says why.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../api/client";
import {
  cancelRun,
  decideRunApproval,
  getRun,
  listSteps,
  pauseRun,
  resumeRun,
  streamRunEvents,
} from "../api/runs";
import { describeAgentError, isFinal } from "../components/agents/labels";
import type { Run, RunControl, Step } from "../types/agents";
import type { ApprovalChoice } from "../types/tools";

export const POLL_INTERVAL_MS = 2_000;
/** A progress stream open at least this long before it closed is opened again, not replaced by polling. */
export const RECONNECT_AFTER_MS = 30_000;

export type RunAction = "pause" | "resume" | "cancel";

/** How the page learns about changes: the progress stream, polling, or not at all (over). */
export type FollowMode = "live" | "polling" | "done";

interface RunState {
  /** Which load produced this state (the task id and the reload count). */
  key: string;
  run: Run | null;
  /** The task does not exist, or it is not the caller's. */
  missing: boolean;
  error: string | null;
}

const CONTROLS: Readonly<Record<RunAction, (id: string) => Promise<RunControl>>> = {
  pause: pauseRun,
  resume: resumeRun,
  cancel: cancelRun,
};

const CONTROL_REFUSED: Readonly<Record<RunAction, string>> = {
  pause: "You cannot pause this task.",
  resume: "You cannot resume this task: you may no longer be allowed to run this agent.",
  cancel: "You cannot cancel this task.",
};

/** Whether `next` is not older than `current` (by `updated_at`). */
function notOlder(next: Run, current: Run | null): boolean {
  if (!current || current.id !== next.id) return true;
  return Date.parse(next.updated_at) >= Date.parse(current.updated_at);
}

/** The steps with `incoming` added or replacing theirs (by id), in order. A finished step never goes back. */
export function mergeSteps(current: readonly Step[], incoming: readonly Step[]): Step[] {
  const byId = new Map(current.map((step) => [step.id, step]));
  for (const step of incoming) {
    const known = byId.get(step.id);
    if (known?.finished_at && !step.finished_at) continue;
    byId.set(step.id, step);
  }
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
}

export function useRun(runId: string) {
  const [state, setState] = useState<RunState | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [mode, setMode] = useState<FollowMode>("live");
  const [reloads, setReloads] = useState(0);
  const [busy, setBusy] = useState<RunAction | null>(null);
  const [deciding, setDeciding] = useState<ApprovalChoice | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const accept = useCallback((run: Run) => {
    setState((current) =>
      current && notOlder(run, current.run) ? { ...current, run, missing: false } : current,
    );
  }, []);

  const acceptSteps = useCallback((incoming: readonly Step[]) => {
    setSteps((current) => mergeSteps(current, incoming));
  }, []);

  useEffect(() => {
    const key = `${runId}|${String(reloads)}`;
    const controller = new AbortController();
    const { signal } = controller;
    let timer = 0;

    const load = () =>
      Promise.all([getRun(runId, { signal }), listSteps(runId, {}, { signal })] as const);

    const gone = (err: unknown): boolean => err instanceof ApiError && err.status === 404;

    const poll = () => {
      timer = window.setTimeout(() => {
        if (document.visibilityState === "hidden") {
          poll();
          return;
        }
        load()
          .then(([run, latest]) => {
            if (signal.aborted) return;
            accept(run);
            acceptSteps(latest);
            if (isFinal(run.status)) setMode("done");
            else poll();
          })
          .catch((err: unknown) => {
            if (signal.aborted) return;
            if (gone(err)) {
              setState((current) => current && { ...current, run: null, missing: true });
              setMode("done");
              return;
            }
            poll(); // a passing failure: the next round asks again
          });
      }, POLL_INTERVAL_MS);
    };

    const follow = async () => {
      for (;;) {
        const opened = Date.now();
        try {
          for await (const event of streamRunEvents(runId, { signal })) {
            if (event.type === "step") {
              acceptSteps([event.step]);
            } else {
              accept(event.run);
              if (event.type === "end") {
                setMode("done");
                return;
              }
            }
          }
        } catch {
          if (signal.aborted) return;
          // A stream that served a while and then closed (the server ends it after an hour)
          // is opened again; one that fails at once gives way to asking every few seconds.
          if (Date.now() - opened >= RECONNECT_AFTER_MS) continue;
          setMode("polling");
          poll();
          return;
        }
      }
    };

    load()
      .then(([run, initial]) => {
        if (signal.aborted) return;
        setState({ key, run, missing: false, error: null });
        setSteps(mergeSteps([], initial));
        if (isFinal(run.status)) {
          setMode("done");
        } else {
          setMode("live");
          void follow();
        }
      })
      .catch((err: unknown) => {
        if (signal.aborted) return;
        const missing = gone(err);
        setState({ key, run: null, missing, error: missing ? null : describeAgentError(err) });
      });

    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [runId, reloads, accept, acceptSteps]);

  /** The task and its steps as they are now; a failure here changes nothing. */
  const refresh = async (): Promise<void> => {
    try {
      const [run, latest] = await Promise.all([getRun(runId), listSteps(runId)]);
      accept(run);
      acceptSteps(latest);
    } catch {
      // The progress stream (or polling) catches up.
    }
  };

  /** Pause, resume or cancel; resolves to whether the server accepted it. */
  const control = async (action: RunAction): Promise<boolean> => {
    setActionError(null);
    setBusy(action);
    let accepted = false;
    try {
      const answer = await CONTROLS[action](runId);
      setNotice(answer.message);
      accepted = true;
    } catch (err) {
      setActionError(describeAgentError(err, CONTROL_REFUSED[action]));
    }
    await refresh();
    setBusy(null);
    return accepted;
  };

  /** Approve or decline the pending action; resolves to whether the decision was taken. */
  const decide = async (choice: ApprovalChoice): Promise<boolean> => {
    const pending = state?.run?.pending_approval;
    if (!pending) return false;
    setActionError(null);
    setDeciding(choice);
    try {
      accept(
        await decideRunApproval(runId, pending.id, {
          decision: choice,
          arguments_hash: pending.arguments_hash,
        }),
      );
      setNotice(choice === "approve" ? "Action approved; the task continues." : "Action declined; the task continues.");
      return true;
    } catch (err) {
      setActionError(describeAgentError(err, "You cannot decide on this action."));
      // Changed, expired or already decided: show the task as it is now.
      if (err instanceof ApiError && (err.status === 409 || err.status === 404)) await refresh();
      return false;
    } finally {
      setDeciding(null);
    }
  };

  const reload = useCallback(() => {
    setReloads((count) => count + 1);
  }, []);

  const current = state?.key === `${runId}|${String(reloads)}` ? state : null;
  return {
    run: state?.run ?? null,
    steps,
    loading: current === null,
    missing: current?.missing === true,
    /** Why the task could not be loaded (not found is `missing`). */
    loadError: current?.error ?? null,
    reload,
    mode,
    busy,
    deciding,
    pause: () => control("pause"),
    resume: () => control("resume"),
    cancel: () => control("cancel"),
    decide,
    actionError,
    dismissActionError: () => {
      setActionError(null);
    },
    /** The last action's outcome, for the live region. */
    notice,
  };
}

export type RunDetail = ReturnType<typeof useRun>;
