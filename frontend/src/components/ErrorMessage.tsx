import { ApiError } from "../api/editions";

export function ErrorMessage({
  error,
  fallback,
}: {
  error: Error;
  fallback: string;
}) {
  return (
    <p role="alert" className="error-message">
      {error instanceof ApiError ? error.message : fallback}
    </p>
  );
}
