/** Bound the entire operation, including body reads and abort-ignoring transports. */
export const REQUEST_TIMEOUT_MS = 30000;
export async function withDeadline(operation, {signal, timeoutMs = REQUEST_TIMEOUT_MS} = {}) {
  const controller = new AbortController();
  let timer, abort;
  const stopped = new Promise((_, reject) => {
    abort = () => {
      const reason = signal?.reason || new DOMException('Request cancelled', 'AbortError');
      controller.abort(reason); reject(reason);
    };
    if (signal?.aborted) { abort(); return; }
    signal?.addEventListener('abort', abort, {once:true});
    timer = setTimeout(() => {
      const reason = new DOMException('Request timed out. Please retry.', 'TimeoutError');
      controller.abort(reason); reject(reason);
    }, timeoutMs);
  });
  try {
    if (controller.signal.aborted) return await stopped;
    return await Promise.race([stopped, Promise.resolve().then(() => operation(controller.signal))]);
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
}
// Consume a raw body before releasing its deadline. Callers retain Response methods.
export async function bufferedResponse(response) {
  if (response.status === 204 || response.status === 205) return response;
  return new Response(await response.arrayBuffer(), {status:response.status,
    statusText:response.statusText, headers:response.headers});
}
