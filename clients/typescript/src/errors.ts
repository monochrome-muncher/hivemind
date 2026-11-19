export class HivemindError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = new.target.name;
    this.code = code;
  }
}

/** The key was rejected (HTTP 401, `unauthenticated`) or is locked (`key_locked`). */
export class AuthenticationError extends HivemindError {}

const AUTH_CODES = new Set(["unauthenticated", "key_locked"]);

export function errorFromEnvelope(code: string, message: string): HivemindError {
  return AUTH_CODES.has(code) ? new AuthenticationError(code, message) : new HivemindError(code, message);
}

/** The error behind a reply that is the `{"error": {"code", "message"}}` envelope, if it is one. */
export function refusal(reply: unknown): HivemindError | undefined {
  if (typeof reply !== "object" || reply === null || Object.keys(reply).join() !== "error") {
    return undefined;
  }
  const error = (reply as { error: unknown }).error;
  if (typeof error !== "object" || error === null) {
    return undefined;
  }
  const { code, message } = error as { code?: unknown; message?: unknown };
  return errorFromEnvelope(String(code || ""), String(message || ""));
}

/** The server's 401 body is `{"error": "key_locked" | "unauthorized", "detail": ...}`. */
export async function rejectedKey(response: Response): Promise<AuthenticationError> {
  const body = (await response.json().catch(() => ({}))) as { error?: unknown; detail?: unknown } | null;
  const code = body?.error === "key_locked" ? "key_locked" : "unauthenticated";
  const detail = typeof body?.detail === "string" && body.detail ? body.detail : "";
  return new AuthenticationError(code, detail || "the server rejected the API key (HTTP 401)");
}
