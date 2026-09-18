// Smoke-test egress guard: one authorization request, no redirects or proxying.
const originalFetch = globalThis.fetch;
let dispatched = false;
globalThis.fetch = async (input, init = {}) => {
  const expected = process.env.LATCH_URL + '/proxy/.well-known/latch-self/authorize';
  let body;
  try { body = JSON.parse(init.body); } catch { throw new Error('Request blocked'); }
  if (dispatched || String(input) !== expected || init.method !== 'POST' ||
      body.method !== 'GET' || body.path !== '/__latch_policy_denial_probe__' ||
      body.body !== undefined || Object.keys(body.headers || {}).length ||
      Object.keys(body).some(k => !['method', 'path', 'headers'].includes(k))) {
    throw new Error('Request blocked');
  }
  dispatched = true;
  return originalFetch(input, {...init, redirect: 'error',
    signal: AbortSignal.timeout(Number(process.env.LATCH_SMOKE_TIMEOUT_MS))});
};
