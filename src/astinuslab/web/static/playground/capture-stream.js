// Pure decision for the live capture SSE stream error handling.
export const MAX_STREAM_ERRORS = 5;

export function captureStreamOnError({
  terminalReached,
  errorCount,
  maxErrors = MAX_STREAM_ERRORS,
}) {
  if (terminalReached) return { action: "close", lost: false };
  if (errorCount >= maxErrors) return { action: "close", lost: true };
  return { action: "retry", lost: false };
}
