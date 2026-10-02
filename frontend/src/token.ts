// The curator token lives in sessionStorage: it survives reloads but is forgotten when the tab
// closes, and it is only ever sent to the Reuse Radar API in an Authorization header.
const KEY = "reuse-radar-curator-token";

export function loadToken(): string {
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    // storage unavailable (private mode): the token stays in memory only
  }
}
