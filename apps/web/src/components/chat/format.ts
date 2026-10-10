/** Number and time formatting for the chat's reply details. */

export const formatCount = (value: number): string => value.toLocaleString();

/** Milliseconds as "480 ms" below a second, "2.2 s" above. */
export function formatDuration(ms: number): string {
  return ms < 1000 ? `${formatCount(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}
