// Exactly one authorization fetch, then at most one governed proxy fetch.
// No direct OpenAI connection, redirects, retries, or other payloads.
const {isDeepStrictEqual} = require('node:util');
const originalFetch = globalThis.fetch;
const body = {model: 'gpt-4o-mini', messages: [{role: 'user', content: 'Hi'}], max_completion_tokens: 1};
let stage = 0;
// Never forward error messages, URLs, headers or arbitrary error codes.
function fetchCategory(error, depth = 0) {
  if (!error || depth > 3) return 'UNKNOWN_FETCH_FAILED';
  if (['ENOTFOUND', 'EAI_AGAIN', 'EAI_FAIL'].includes(error.code)) return 'DNS_FAILED';
  if (['ECONNREFUSED', 'ECONNRESET', 'ENETUNREACH', 'EHOSTUNREACH', 'EACCES', 'EPERM', 'UND_ERR_SOCKET'].includes(error.code)) return 'CONNECT_FAILED';
  if (['CERT_HAS_EXPIRED', 'CERT_NOT_YET_VALID', 'DEPTH_ZERO_SELF_SIGNED_CERT',
       'SELF_SIGNED_CERT_IN_CHAIN', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
       'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'ERR_TLS_CERT_ALTNAME_INVALID',
       'ERR_SSL_WRONG_VERSION_NUMBER'].includes(error.code)) return 'TLS_FAILED';
  if (error.name === 'TimeoutError' || ['ETIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT',
       'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT'].includes(error.code)) return 'FETCH_TIMEOUT';
  if (Array.isArray(error.errors) && error.errors.length > 0 && error.errors.length <= 8) {
    const categories = error.errors.map(item => fetchCategory(item, depth + 1));
    return categories.every(item => item === categories[0]) ? categories[0] : 'UNKNOWN_FETCH_FAILED';
  }
  return fetchCategory(error.cause, depth + 1);
}
globalThis.fetch = async (input, init = {}) => {
  let parsed;
  try { parsed = JSON.parse(init.body); } catch { throw new Error('Request blocked'); }
  const base = process.env.LATCH_URL;
  const auth = {method: 'POST', path: '/v1/chat/completions', headers: {}, body};
  const authorize = stage === 0 && String(input) === base + '/proxy/.well-known/latch-self/authorize' && isDeepStrictEqual(parsed, auth);
  const proxy = stage === 1 && String(input) === base + '/proxy/v1/chat/completions' && isDeepStrictEqual(parsed, body);
  if (init.method !== 'POST' || (!authorize && !proxy)) throw new Error('Request blocked');
  stage = 2; // Failure or DENY never permits another fetch.
  const options = {...init, redirect: 'error',
    signal: AbortSignal.timeout(Number(process.env.LATCH_SMOKE_TIMEOUT_MS))};
  let response;
  try {
    response = await originalFetch(input, options);
  } catch (error) {
    let category = 'UNKNOWN_FETCH_FAILED';
    try { category = fetchCategory(error); } catch { /* Unreadable cause stays unknown. */ }
    // Existing MCP handler forwards only this fixed message. No original cause attached.
    throw new Error(`LATCH_SAFE_FETCH:${authorize ? 'AUTHORIZE' : 'PROXY'}:${category}`);
  }
  if (authorize && response.ok) {
    const grant = await response.clone().json();
    if (grant && grant.authorized === true && !grant.receipt &&
        !('deniedBy' in grant) && !('reason' in grant) && !('decision' in grant)) stage = 1;
  }
  return response;
};
