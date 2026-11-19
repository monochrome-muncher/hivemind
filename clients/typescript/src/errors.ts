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
