/**
 * How much of its budget a task has used: steps, tool calls and tokens against the agent's
 * limits, and the time it has left. While its clock runs (from its start, and again once a
 * pause or an approval is over), the time counts down to `deadline_at`; while it is paused or
 * waiting for approval its clock is stopped and `time_left_seconds` says what is left. Each
 * meter says its numbers in words; the bar under them is decoration.
 */

import type { Run } from "../../types/agents";
import { formatCount } from "../chat/format";
import { formatSeconds, isFinal } from "./labels";

/** Above this share of a limit, the bar turns to the warning colour. */
const NEARLY_USED = 0.85;

function Meter({ label, value, share }: { label: string; value: string; share: number | null }) {
  const filled = share === null ? null : Math.min(1, Math.max(0, share));
  return (
    <div className="run-meter">
      <dt>{label}</dt>
      <dd>
        <span className="run-meter-value">{value}</span>
        {filled !== null && (
          <span className="run-meter-bar" aria-hidden="true">
            <span
              className={`run-meter-fill${filled >= NEARLY_USED ? " run-meter-high" : ""}`}
              style={{ width: `${String(Math.round(filled * 100))}%` }}
            />
          </span>
        )}
      </dd>
    </div>
  );
}

const of = (used: number, limit: number): string => `${formatCount(used)} of ${formatCount(limit)}`;

const share = (used: number, limit: number): number | null => (limit > 0 ? used / limit : null);

/** The time line: what is left (counting down, or with the clock stopped), or the limit. */
function timeMeter(run: Run, now: number | null): { value: string; share: number | null } {
  const limit = run.limits.max_duration_seconds;
  if (isFinal(run.status)) return { value: `${formatSeconds(limit)} limit`, share: null };
  if (run.time_left_seconds !== null) {
    return {
      value: `${formatSeconds(run.time_left_seconds)} left (clock stopped)`,
      share: share(limit - run.time_left_seconds, limit),
    };
  }
  if (run.deadline_at !== null) {
    const deadline = Date.parse(run.deadline_at);
    if (now === null || Number.isNaN(deadline)) return { value: `${formatSeconds(limit)} limit`, share: null };
    const left = Math.max(0, (deadline - now) / 1000);
    return { value: `${formatSeconds(left)} left`, share: share(limit - left, limit) };
  }
  return { value: `${formatSeconds(limit)}, from when it starts`, share: 0 };
}

export function RunBudget({ run, now }: { run: Run; now: number | null }) {
  const tokens = run.usage.input_tokens + run.usage.output_tokens;
  const time = timeMeter(run, now);
  return (
    <dl className="run-budget">
      <Meter
        label="Steps"
        value={of(run.step_count, run.limits.max_steps)}
        share={share(run.step_count, run.limits.max_steps)}
      />
      <Meter
        label="Tool calls"
        value={of(run.tool_call_count, run.limits.max_tool_calls)}
        share={share(run.tool_call_count, run.limits.max_tool_calls)}
      />
      <Meter label="Tokens" value={of(tokens, run.limits.max_tokens)} share={share(tokens, run.limits.max_tokens)} />
      <Meter label="Time" value={time.value} share={time.share} />
    </dl>
  );
}
