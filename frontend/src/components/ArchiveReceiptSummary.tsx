import type { ArchiveReceipt } from "../api/collection";

export function ArchiveReceiptSummary({
  receipt,
}: {
  receipt: ArchiveReceipt;
}) {
  return (
    <dl className="archive-receipt" aria-label="Reçu d’archive manuelle">
      <div>
        <dt>État</dt>
        <dd>Références à reconstruire</dd>
      </div>
      <div>
        <dt>Taille</dt>
        <dd>{receipt.bytes} octets</dd>
      </div>
      <div>
        <dt>MIME</dt>
        <dd>{receipt.detected_mime_type}</dd>
      </div>
      <div>
        <dt>SHA-256</dt>
        <dd>
          <code>{receipt.decoded_sha256.slice(0, 12)}…</code>
        </dd>
      </div>
      <div>
        <dt>Date</dt>
        <dd>{new Date(receipt.completed_at).toLocaleString("fr-FR")}</dd>
      </div>
      <div>
        <dt>Analyste</dt>
        <dd>{receipt.actor_id}</dd>
      </div>
    </dl>
  );
}
