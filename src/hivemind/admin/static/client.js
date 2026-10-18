// Hivemind front-end kit: the API client. One key per browser tab (in
// sessionStorage, with an in-memory fallback), sent as X-API-Key to the
// same-origin proxy and nowhere else. Each app makes its own client with
// its own storage slot, so an admin key and an agent key never share one.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** A client bound to one sessionStorage slot. `appName` names the app in a network error. */
export function createClient({ storageKey, appName }) {
  let memoryKey = null; // fallback when sessionStorage is unavailable

  function getKey() {
    try { return sessionStorage.getItem(storageKey); } catch { return null; }
  }
  function setKey(key) {
    try { sessionStorage.setItem(storageKey, key); } catch { /* private mode */ }
    memoryKey = key;
  }
  function clearKey() {
    try { sessionStorage.removeItem(storageKey); } catch { /* ignore */ }
    memoryKey = null;
  }
  const hasKey = () => Boolean(getKey() || memoryKey);

  /** Like api(), but resolves to {payload, headers}. */
  async function apiRaw(method, path, { body, query, key } = {}) {
    const url = new URL(`/v1/${path}`, window.location.origin);
    for (const [k, v] of Object.entries(query || {})) {
      if (v !== undefined && v !== null && v !== "" && v !== false) url.searchParams.set(k, v);
    }
    const headers = { "X-API-Key": key || getKey() || memoryKey || "", Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "omit",
      });
    } catch {
      throw new ApiError(0, "network", `${appName} could not be reached.`);
    }
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = null; }
    }
    if (!response.ok) {
      const err = (payload && payload.error) || {};
      let message = err.message || (payload && typeof payload.detail === "string" ? payload.detail : "");
      if (!message && payload && Array.isArray(payload.detail)) {
        message = payload.detail.map((d) => d.msg).join("; ");
      }
      throw new ApiError(response.status, err.code || "http_" + response.status, message || response.statusText);
    }
    return { payload, headers: response.headers };
  }

  async function api(method, path, options) {
    return (await apiRaw(method, path, options)).payload;
  }

  return { getKey, setKey, clearKey, hasKey, apiRaw, api };
}
