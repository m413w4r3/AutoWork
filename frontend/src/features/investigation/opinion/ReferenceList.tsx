export function ReferenceList({
  title,
  ids,
  resolve,
}: {
  title: string;
  ids: string[];
  resolve: (id: string) => string;
}) {
  return (
    <div>
      <h5>{title}</h5>
      {ids.length === 0 ? (
        <p>Aucune référence citée.</p>
      ) : (
        <ul>
          {ids.map((id) => (
            <li key={id}>
              <code>{id}</code> — {resolve(id)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
