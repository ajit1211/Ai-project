/** The chat's line icons (24×24, drawn with the current text colour). Decorative: the button carries the name. */

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
