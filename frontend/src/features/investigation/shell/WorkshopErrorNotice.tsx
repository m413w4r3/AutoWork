/** The single typed-error notice shared by the cockpit views. */

import { workshopErrorText } from "./stateText";

export function WorkshopErrorNotice({
  label,
  error,
}: {
  label: string;
  error: unknown;
}) {
  return (
    <p className="error-message" role="alert">
      {workshopErrorText(error, label)}
    </p>
  );
}
