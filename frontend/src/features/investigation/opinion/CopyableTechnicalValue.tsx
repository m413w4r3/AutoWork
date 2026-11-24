import { useState } from "react";

export function CopyableTechnicalValue({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  const [copyStatus, setCopyStatus] = useState("");

  async function copyValue() {
    try {
      if (navigator.clipboard?.writeText === undefined) {
        setCopyStatus("Copie indisponible dans ce navigateur.");
        return;
      }
      await navigator.clipboard.writeText(value);
      setCopyStatus("Copié.");
    } catch {
      setCopyStatus("Copie impossible.");
    }
  }

  return (
    <span className="opinion-technical-value">
      <code>{value}</code>
      <button
        type="button"
        className="button button--secondary opinion-technical-value__copy"
        aria-label={"Copier " + label}
        onClick={() => void copyValue()}
      >
        Copier
      </button>
      <span className="opinion-technical-value__status" role="status">
        {copyStatus}
      </span>
    </span>
  );
}
