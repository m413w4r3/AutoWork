import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getEditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { EditorialDocument } from "./EditorialDocument";

interface EditorialWorkspacePageProps {
  subjectId: string;
}

export function EditorialWorkspacePage({
  subjectId,
}: EditorialWorkspacePageProps) {
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const snapshotQuery = useQuery({
    queryKey: ["editorial-workspace", subjectId],
    queryFn: () => getEditorialWorkspaceSnapshot(subjectId),
  });

  if (snapshotQuery.isPending) {
    return <p role="status">Chargement de l’atelier éditorial…</p>;
  }
  if (snapshotQuery.isError) {
    return <p role="alert">{snapshotQuery.error.message}</p>;
  }

  return (
    <main>
      <h1>Atelier éditorial</h1>
      <EditorialDocument
        snapshot={snapshotQuery.data}
        selectedBlockId={selectedBlockId}
        onSelectBlock={setSelectedBlockId}
      />
    </main>
  );
}
