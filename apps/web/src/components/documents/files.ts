/**
 * The library's file helpers: sizes in human units, the extension allowlist, and the checks a
 * file must pass before it is sent. They only spare the user a pointless upload: the server
 * checks the extension, the size and the content again.
 */

/** The server's allowlist (documents/extraction/sniff.py), used until its first answer. */
export const DEFAULT_ACCEPTED_EXTENSIONS: readonly string[] = [
  ".pdf",
  ".docx",
  ".txt",
  ".md",
  ".markdown",
  ".csv",
];

/** What the client checks before uploading. A null size limit is not known yet. */
export interface UploadRules {
  accepted_extensions: readonly string[];
  max_upload_bytes: number | null;
}

const UNITS = ["KB", "MB", "GB", "TB"] as const;

/** "512 B", "1.5 KB", "2.4 MB", "50 MB": one decimal below ten, none above (1 KB = 1024 B). */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes.toLocaleString()} B`;
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value < 10 ? 1 : 0;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: digits })} ${UNITS[unit] ?? "TB"}`;
}

/** The lowercase extension with its dot (".pdf"), or "" when the name has none. */
export function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot > 0 && dot < filename.length - 1 ? filename.slice(dot).toLowerCase() : "";
}

/** ".pdf, .docx or .csv" */
export function listExtensions(extensions: readonly string[]): string {
  return new Intl.ListFormat("en", { style: "long", type: "disjunction" }).format(extensions);
}

/** Why `file` cannot be uploaded, or null when it may be sent. */
export function checkFile(file: Pick<File, "name" | "size">, rules: UploadRules): string | null {
  const extension = extensionOf(file.name);
  if (!extension || !rules.accepted_extensions.includes(extension)) {
    return `This file type is not supported. Upload a ${listExtensions(rules.accepted_extensions)} file.`;
  }
  if (file.size === 0) return "The file is empty.";
  if (rules.max_upload_bytes !== null && file.size > rules.max_upload_bytes) {
    return `The file is ${formatBytes(file.size)}; the limit is ${formatBytes(rules.max_upload_bytes)}.`;
  }
  return null;
}
