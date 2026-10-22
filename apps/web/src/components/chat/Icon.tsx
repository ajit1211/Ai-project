/**
 * NEXA's line icons (24×24, drawn with the current text colour), first made for the chat and
 * shared by the document and agent pages. Decorative: the button or link carries the name.
 */

const PATHS = {
  menu: ["M3 6h18", "M3 12h18", "M3 18h18"],
  close: ["M18 6 6 18", "M6 6l12 12"],
  plus: ["M12 5v14", "M5 12h14"],
  pencil: ["M12 20h9", "M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"],
  archive: ["M3 4h18v4H3z", "M5 8v12h14V8", "M10 12h4"],
  unarchive: ["M3 12a9 9 0 1 0 3-6.7L3 8", "M3 3v5h5"],
  trash: ["M3 6h18", "M8 6V4h8v2", "M19 6l-1 14H6L5 6", "M10 11v6", "M14 11v6"],
  copy: ["M9 9h11v11H9z", "M5 15H4V4h11v1"],
  check: ["M20 6 9 17l-5-5"],
  refresh: ["M21 12a9 9 0 1 1-3-6.7L21 8", "M21 3v5h-5"],
  stop: ["M7 7h10v10H7z"],
  send: ["M22 2 11 13", "M22 2 15 22l-4-9-9-4Z"],
  upload: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "M17 8l-5-5-5 5", "M12 3v12"],
  download: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "M7 10l5 5 5-5", "M12 15V3"],
  file: [
    "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z",
    "M14 2v6h6",
    "M16 13H8",
    "M16 17H8",
    "M10 9H8",
  ],
  search: ["M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z", "M21 21l-4.35-4.35"],
  back: ["M19 12H5", "M12 19l-7-7 7-7"],
  // AI tool calls: failed, refused or stopped, timed out, waiting for approval.
  alert: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M12 8v5", "M12 16h.01"],
  ban: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M5.6 5.6l12.8 12.8"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M12 7v5l3 2"],
  shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z", "M12 8v4", "M12 15h.01"],
  // Agent tasks: a decision, a tool call, a progress note; pausing and resuming.
  spark: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z", "M19 17v4", "M17 19h4"],
  tool: [
    "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9Z",
  ],
  message: ["M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"],
  pause: ["M8 5v14", "M16 5v14"],
  play: ["M7 4l13 8-13 8Z"],
} as const satisfies Record<string, readonly string[]>;

export type IconName = keyof typeof PATHS;

export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
