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

function sanitizeParsedUrl(value: string): string | null {
  try {
    const url = new URL(value);
    url.username = "";
    url.password = "";
    url.search = redactQuery(url.search.slice(1));
    url.hash = "";
    return url.toString();
  } catch {
    return null;
  }
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
  const parsed = sanitizeParsedUrl(value);
  if (parsed !== null) return parsed;

  const withEmbeddedUrls = value.replace(
    /(?:https?|ftp):\/\/[^\s"'<>]+/gi,
    (candidate) =>
      sanitizeParsedUrl(candidate) ?? sanitizeMalformedUrl(candidate),
  );
  if (withEmbeddedUrls !== value && !isUrlLike(value)) return withEmbeddedUrls;
  return isUrlLike(value) ? sanitizeMalformedUrl(withEmbeddedUrls) : value;
}
