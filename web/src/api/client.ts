/**
 * The single request boundary.
 *
 * Everything the UI knows about the backend arrives through this file. It normalises
 * failure and does nothing else: there is no retry policy, no cache, no safety judgement and
 * no fallback to invented data here. A request either produces the typed body the backend
 * sent, or an `ApiError` saying precisely what went wrong.
 *
 * The distinction this file exists to preserve: **an HTTP failure is not a domain state.**
 * A 200 carrying `protection.status: "unknown"` is a success — the backend answered, and its
 * answer is "unknown". A 503 is a failure. Collapsing the two would let a network problem
 * render as a fact about the host.
 */
import type { ErrorBody } from './types';

export const API_BASE = '/api/v1';

/** Why a request did not produce a body. Each kind gets its own operator-facing treatment. */
export type ApiErrorKind =
  /** The backend answered with a structured `{error:{code,message,detail}}` envelope. */
  | 'backend'
  /** The request never reached a backend: DNS, connection refused, offline. */
  | 'unreachable'
  /** The request exceeded its deadline and was aborted. */
  | 'timeout'
  /** The caller aborted — navigation, unmount. Never shown to an operator. */
  | 'cancelled'
  /** A response arrived that this build cannot read: not JSON, or not the shape declared. */
  | 'malformed';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** The backend's stable machine-readable code, when it sent one. Branch on this, not text. */
  readonly code: string | null;
  readonly status: number | null;
  readonly detail: Record<string, unknown>;
  readonly path: string;

  constructor(init: {
    kind: ApiErrorKind;
    message: string;
    path: string;
    code?: string | null;
    status?: number | null;
    detail?: Record<string, unknown>;
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.kind = init.kind;
    this.code = init.code ?? null;
    this.status = init.status ?? null;
    this.detail = init.detail ?? {};
    this.path = init.path;
  }

  /** True when retrying the identical request could plausibly succeed later. */
  get retryable(): boolean {
    if (this.kind === 'unreachable' || this.kind === 'timeout') return true;
    if (this.kind !== 'backend' || this.status === null) return false;
    return this.status >= 500;
  }

  /** True when the backend answered but this object does not exist. */
  get notFound(): boolean {
    return this.status === 404;
  }
}

function isErrorEnvelope(value: unknown): value is { error: ErrorBody } {
  if (typeof value !== 'object' || value === null || !('error' in value)) return false;
  const error: unknown = value.error;
  return typeof error === 'object' && error !== null && 'code' in error;
}

export interface RequestOptions {
  signal?: AbortSignal | undefined;
  /** Milliseconds before the request is abandoned. Defaults to 15s. */
  timeoutMs?: number | undefined;
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | undefined;
  /** A one-request credential exchange. Callers must not retain this value. */
  bearer?: string | undefined;
  query?: Record<string, string | number | undefined> | undefined;
  body?: unknown;
}

const DEFAULT_TIMEOUT_MS = 15_000;

type AuthenticationFailureHandler = () => void;
let authenticationFailureHandler: AuthenticationFailureHandler | null = null;

/** Register the shell boundary that handles expired or revoked sessions. */
export function setAuthenticationFailureHandler(
  handler: AuthenticationFailureHandler | null,
): void {
  authenticationFailureHandler = handler;
}

function buildUrl(path: string, query: RequestOptions['query']): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const serialised = params.toString();
  return serialised ? `${url}?${serialised}` : url;
}

/**
 * Perform one request and return its typed body.
 *
 * The response is not validated field-by-field against the schema. Types come from the
 * backend's own OpenAPI document, so the compiler already holds the contract; a runtime
 * re-check would duplicate it and would still not make an unexpected body safe to use. What
 * *is* checked is that a body arrived and parsed — a non-JSON response is `malformed`, not a
 * silently empty object.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { signal, timeoutMs = DEFAULT_TIMEOUT_MS, method = 'GET', bearer, query, body } = options;
  const url = buildUrl(path, query);

  const timeout = AbortSignal.timeout(timeoutMs);
  const composed = signal ? anySignal([signal, timeout]) : timeout;

  let response: Response;
  try {
    const headers: Record<string, string> = { accept: 'application/json' };
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (bearer !== undefined) headers.authorization = `Bearer ${bearer}`;
    response = await fetch(url, {
      method,
      signal: composed,
      credentials: 'same-origin',
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (cause) {
    // A caller's own abort and a deadline are different events and read differently to an
    // operator: one is "you navigated away", the other is "the backend did not answer".
    if (signal?.aborted) {
      throw new ApiError({ kind: 'cancelled', message: 'request cancelled', path });
    }
    if (timeout.aborted) {
      throw new ApiError({
        kind: 'timeout',
        message: `no response within ${timeoutMs} ms`,
        path,
      });
    }
    throw new ApiError({
      kind: 'unreachable',
      message: cause instanceof Error ? cause.message : 'the backend could not be reached',
      path,
    });
  }

  const text = await response.text();
  let parsed: unknown = undefined;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      parsed = undefined;
    }
  }

  if (!response.ok) {
    if (response.status === 401 && path !== '/session') authenticationFailureHandler?.();
    if (isErrorEnvelope(parsed)) {
      throw new ApiError({
        kind: 'backend',
        message: parsed.error.message || `request failed with ${response.status}`,
        path,
        code: parsed.error.code,
        status: response.status,
        detail: parsed.error.detail ?? {},
      });
    }
    throw new ApiError({
      kind: 'backend',
      message: `request failed with ${response.status}`,
      path,
      code: `http_${response.status}`,
      status: response.status,
    });
  }

  if (response.status === 204) return undefined as T;

  if (parsed === undefined) {
    throw new ApiError({
      kind: 'malformed',
      message: 'the backend answered with a body this build could not read as JSON',
      path,
      status: response.status,
    });
  }

  return parsed as T;
}

/** `AbortSignal.any` is not available on every runtime this build targets. */
function anySignal(signals: AbortSignal[]): AbortSignal {
  const controller = new AbortController();
  const abort = (): void => controller.abort();
  for (const signal of signals) {
    if (signal.aborted) {
      controller.abort();
      break;
    }
    signal.addEventListener('abort', abort, { once: true });
  }
  return controller.signal;
}
