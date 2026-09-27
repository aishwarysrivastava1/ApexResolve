// The only place that talks to the server. Every call sends the bearer token and turns
// RFC 9457 problem responses into an ApiError with a readable message.
import type { Problem, Role, User } from "./types";

// The access token lives only in memory (never localStorage): reloading the page means signing in again.
let accessToken: string | null = null;
// Called when the server rejects a signed-in session (401), so the app can return to the sign-in screen.
let sessionEnded: () => void = () => {};

export function setToken(token: string | null): void {
  accessToken = token;
}

export function onSessionEnded(handler: () => void): void {
  sessionEnded = handler;
}

export class ApiError extends Error {
  status: number;
  problem: Problem | null;

  constructor(status: number, problem: Problem | null) {
    // prefer the server's plain-language detail; fall back to the HTTP status
    super(problem?.detail || problem?.title || `Request failed (${status})`);
    this.status = status;
    this.problem = problem;
  }
}

async function send<T>(method: string, path: string, body?: BodyInit, headers: Record<string, string> = {}): Promise<T> {
  // 1. attach the token when signed in
  const allHeaders: Record<string, string> = { ...headers };
  if (accessToken) {
    allHeaders["Authorization"] = `Bearer ${accessToken}`;
  }
  // 2. make the request (same origin: Caddy serves the app and proxies /api and /idp)
  const response = await fetch(path, { method, headers: allHeaders, body });
  // 3. 204 No Content has no body
  if (response.status === 204) {
    return undefined as T;
  }
  // 4. read JSON bodies (both normal and problem+json)
  const contentType = response.headers.get("content-type") ?? "";
  const data = contentType.includes("json") ? await response.json() : null;
  // 5. an expired or rejected token ends the session (sign-in itself has no token, so it is not affected)
  if (response.status === 401 && accessToken !== null) {
    sessionEnded();
  }
  // 6. anything other than 2xx becomes an ApiError
  if (!response.ok) {
    throw new ApiError(response.status, data as Problem | null);
  }
  return data as T;
}

export function getJson<T>(path: string): Promise<T> {
  return send<T>("GET", path);
}

export function postJson<T>(path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const text = body === undefined ? undefined : JSON.stringify(body);
  return send<T>("POST", path, text, { "Content-Type": "application/json", ...headers });
}

export function postForm<T>(path: string, form: FormData): Promise<T> {
  // the browser sets the multipart boundary itself, so no Content-Type header here
  return send<T>("POST", path, form);
}

export function postText<T>(path: string, text: string): Promise<T> {
  return send<T>("POST", path, text, { "Content-Type": "application/json" });
}

// Turn any thrown value into one line of text for the screen.
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return "Your session has expired. Please sign in again.";
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

// Evidence files are downloaded with the token, then saved through a temporary object URL.
export async function downloadFile(path: string, fileName: string): Promise<void> {
  const headers: Record<string, string> = {};
  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }
  const response = await fetch(path, { headers });
  if (!response.ok) {
    throw new ApiError(response.status, null);
  }
  const blob = await response.blob();
  saveBlob(blob, fileName);
}

export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}

// ---- sign-in through the development IdP (dev/demo only) ----

interface TokenResponse {
  access_token: string;
}

interface Claims {
  sub: string;
  role: Role;
  name: string;
  merchant_id?: string;
}

// Read the token's claims for display. This is NOT verification: the API verifies every request.
function readClaims(token: string): Claims {
  const payload = token.split(".")[1];
  const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes)) as Claims;
}

export async function signIn(username: string, password: string): Promise<User> {
  const result = await postJson<TokenResponse>("/idp/token", { username, password });
  const claims = readClaims(result.access_token);
  setToken(result.access_token);
  return {
    token: result.access_token,
    sub: claims.sub,
    role: claims.role,
    name: claims.name,
    merchantId: claims.merchant_id ?? null,
  };
}
