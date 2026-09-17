import { describe, expect, it } from "vitest";

import * as legacyEditionWorkflow from "./EditionWorkflow";

// AW-004 : garde-fou tant que le fichier vide n’a pas été retiré du dépôt.
// L’ancienne composition linéaire de l’Edition n’existe plus : la vue
// d’ensemble et les capacités sont couvertes par EditionDashboard,
// EditionWorkspace, App et EditionDetailPage.
describe("ancienne composition linéaire de l’Edition", () => {
  it("n’expose plus aucune surface", () => {
    expect(Object.keys(legacyEditionWorkflow)).toEqual([]);
  });
});
