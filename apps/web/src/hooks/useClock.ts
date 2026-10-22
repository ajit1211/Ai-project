/**
 * The current time, refreshed every second while `running`, for countdowns. It is read in a
 * timer, never during rendering, so it is null until the first tick (right after mounting).
 */

import { useEffect, useState } from "react";

export const TICK_MS = 1_000;

export function useClock(running: boolean): number | null {
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    if (!running) return;
    const tick = () => {
      setNow(Date.now());
    };
    const first = window.setTimeout(tick, 0);
    const timer = window.setInterval(tick, TICK_MS);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [running]);

  return now;
}
