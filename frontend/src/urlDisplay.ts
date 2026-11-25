const SENSITIVE_QUERY_KEYS = new Set([
  "token",
  "key",
  "apikey",
  "api_key",
  "access_token",
  "signature",
  "sig",
  "expires",
  "password",
  "secret",
]);

const NON_HTTP_SCHEME_PLACEHOLDER = "URL non affichable";

function isSensitiveQueryKey(key: string): boolean {
  const normalized = key.toLowerCase();
  return (
    SENSITIVE_QUERY_KEYS.has(normalized) ||
    normalized.startsWith("x-amz-") ||
    normalized.startsWith("x-goog-")
  );
}

function redactQuery(query: string): string {
  const redacted = new URLSearchParams();
  for (const [key, value] of new URLSearchParams(query)) {
    redacted.append(key, isSensitiveQueryKey(key) ? "REDACTED" : value);
  }
  return redacted.toString();
}

function sanitizeHttpUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    url.username = "";
    url.password = "";
    url.search = redactQuery(url.search.slice(1));
    url.hash = "";
    return url.toString();
  } catch {
    return null;
  }
}

/** Return a navigable, redacted href only for absolute HTTP(S) URLs. */
export function safeHttpHref(value: string): string | null {
  return sanitizeHttpUrl(value);
}

function isNonHttpSchemeUrl(value: string): boolean {
  const trimmed = value.trim();
  const colonIndex = trimmed.indexOf(":");
  if (colonIndex <= 0) return false;
  const scheme = trimmed
    .slice(0, colonIndex)
    .split("")
    .filter((character) => {
      const code = character.charCodeAt(0);
      return code > 0x20 && code !== 0x7f;
    })
    .join("");
  if (!/^[a-z][a-z\d+.-]*$/i.test(scheme)) return false;
  if (/^https?$/i.test(scheme)) return false;

  const remainder = trimmed.slice(colonIndex + 1);
  const hasOrdinarySpace = /[\u0020]/.test(trimmed.slice(0, colonIndex));
  const knownScheme =
    /^(?:javascript|data|vbscript|file|ftp|mailto|blob|filesystem)$/i.test(
      scheme,
    );
  if (hasOrdinarySpace && !knownScheme) return false;
  return knownScheme || remainder.startsWith("//") || !/\s/.test(remainder);
}

function sanitizeMalformedUrl(value: string): string {
  const fragmentIndex = value.indexOf("#");
  const withoutFragment =
    fragmentIndex < 0 ? value : value.slice(0, fragmentIndex);
  const queryIndex = withoutFragment.indexOf("?");
  const prefix =
    queryIndex < 0 ? withoutFragment : withoutFragment.slice(0, queryIndex);
  const withoutUserInfo = prefix.replace(
    /^((?:[a-z][a-z\d+.-]*:)?\/\/)[^/?#]*@/i,
    "$1",
  );
  if (queryIndex < 0) return withoutUserInfo;

  const query = withoutFragment.slice(queryIndex + 1);
  return `${withoutUserInfo}?${redactQuery(query)}`;
}

function isUrlLike(value: string): boolean {
  const trimmed = value.trim();
  return (
    /^(?:[a-z][a-z\d+.-]*:\/\/|\/\/|\/)/i.test(trimmed) ||
    /^[^\s/?#]+\.[^\s/?#]+(?:[/?#]|$)/.test(trimmed) ||
    (/^\S+$/.test(trimmed) && /[/?#]/.test(trimmed))
  );
}

/** Remove URL credentials/fragments and redact secrets while retaining identifiers. */
export function sanitizeUrlForDisplay(value: string): string {
  const httpUrl = sanitizeHttpUrl(value);
  if (httpUrl !== null) return httpUrl;
  if (isNonHttpSchemeUrl(value)) return NON_HTTP_SCHEME_PLACEHOLDER;

  const withEmbeddedUrls = value.replace(
    /(?:https?|ftp):\/\/[^\s"'<>]+/gi,
    (candidate) => {
      const safeHref = sanitizeHttpUrl(candidate);
      return (
        safeHref ??
        (isNonHttpSchemeUrl(candidate)
          ? NON_HTTP_SCHEME_PLACEHOLDER
          : sanitizeMalformedUrl(candidate))
      );
    },
  );
  if (withEmbeddedUrls !== value && !isUrlLike(value)) return withEmbeddedUrls;
  return isUrlLike(value) ? sanitizeMalformedUrl(withEmbeddedUrls) : value;
}

/** Redact embedded URLs and cap operator-facing free text. */
export function sanitizeTextForDisplay(value: string): string {
  return sanitizeUrlForDisplay(value).slice(0, 500);
}
