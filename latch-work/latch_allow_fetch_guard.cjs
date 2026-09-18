// Exactly one authorization fetch, then at most one governed proxy fetch.
// No direct OpenAI connection, redirects, retries, or other payloads.
const {isDeepStrictEqual} = require('node:util');
const originalFetch = globalThis.fetch;
const body = {model: 'gpt-4o-mini', messages: [{role: 'user', content: 'Hi'}], max_completion_tokens: 1};
let stage = 0;
globalThis.fetch = async (input, init = {}) => {
  let parsed;
  try { parsed = JSON.parse(init.body); } catch { throw new Error('Request blocked'); }
  const base = process.env.LATCH_URL;
  const auth = {method: 'POST', path: '/v1/chat/completions', headers: {}, body};
  const authorize = stage === 0 && String(input) === base + '/proxy/.well-known/latch-self/authorize' && isDeepStrictEqual(parsed, auth);
  const proxy = stage === 1 && String(input) === base + '/proxy/v1/chat/completions' && isDeepStrictEqual(parsed, body);
  if (init.method !== 'POST' || (!authorize && !proxy)) throw new Error('Request blocked');
  stage = 2; // Failure or DENY never permits another fetch.
  const response = await originalFetch(input, {...init, redirect: 'error',
    signal: AbortSignal.timeout(Number(process.env.LATCH_SMOKE_TIMEOUT_MS))});
  if (authorize && response.ok) {
    const grant = await response.clone().json();
    if (grant && grant.authorized === true && !grant.receipt &&
        !('deniedBy' in grant) && !('reason' in grant) && !('decision' in grant)) stage = 1;
  }
  return response;
};
