export interface ApiProxyOptions {
  target: string;
  changeOrigin: true;
  headers?: Record<string, string>;
}

export function createApiProxyOptions(
  target: string,
  operatorApiToken: string | undefined,
): ApiProxyOptions {
  return {
    target,
    changeOrigin: true,
    ...(operatorApiToken
      ? { headers: { Authorization: `Bearer ${operatorApiToken}` } }
      : {}),
  };
}
