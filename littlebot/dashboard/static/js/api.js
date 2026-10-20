// JSON API client: same-origin cookies, CSRF header on every mutation,
// friendly error messages, and a hook for "session expired".

let csrfToken = null;
let unauthorizedHandler = null;

export class ApiError extends Error {
  constructor(status, message, code, data) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

export function setCsrf(token) { csrfToken = token || null; }
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

const FRIENDLY = {
  0: "Can't reach LittleBot. Is it switched on and are you on the same Wi-Fi?",
  429: 'Too many attempts. Please wait a minute and try again.',
  500: 'Something went wrong on LittleBot. Please try again.',
  503: 'That part of LittleBot is not available right now.',
};

export async function api(path, { method = 'GET', body, signal, timeout = 20000 } = {}) {
  const headers = { Accept: 'application/json' };
  const opts = { method, headers, credentials: 'same-origin', cache: 'no-store', signal };
  if (method !== 'GET' && method !== 'HEAD') {
    headers['Content-Type'] = 'application/json';
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    opts.body = JSON.stringify(body ?? {});
  }
  let timer = null;
  if (!signal && timeout) {
    const ctl = new AbortController();
    opts.signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), timeout);
  }
  let res;
  try {
    res = await fetch(`/api${path}`, opts);
  } catch (e) {
    throw new ApiError(0, FRIENDLY[0], 'network');
  } finally {
    if (timer) clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const detail = (data && typeof data.detail === 'string' && data.detail) || FRIENDLY[res.status] || `Request failed (${res.status}).`;
    const err = new ApiError(res.status, detail, data && data.code, data);
    if (res.status === 401 && data && data.code === 'auth_required' && unauthorizedHandler) unauthorizedHandler();
    throw err;
  }
  return data;
}

export const get = (p, o) => api(p, { ...o, method: 'GET' });
export const post = (p, body, o) => api(p, { ...o, method: 'POST', body });
export const put = (p, body, o) => api(p, { ...o, method: 'PUT', body });
export const patch = (p, body, o) => api(p, { ...o, method: 'PATCH', body });
export const del = (p, o) => api(p, { ...o, method: 'DELETE' });
