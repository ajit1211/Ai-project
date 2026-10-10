/** Three dots that pulse while a reply is being generated (static when motion is reduced). */
export function StreamingIndicator() {
  return (
    <span className="streaming-indicator">
      <span className="streaming-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <span className="visually-hidden">Generating…</span>
    </span>
  );
}
