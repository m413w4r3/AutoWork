import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { ProductionStageCard } from "./ProductionStageCard";

it("signale une étape réutilisée avec son statut de provenance", () => {
  const { container } = render(
    <ol>
      <ProductionStageCard
        stage="extraction"
        status="succeeded"
        stageNumber={3}
        reused
        detail="1 artifact"
      />
    </ol>,
  );

  expect(container.querySelector(".production-stage.is-reused")).toBeTruthy();
  expect(screen.getByText(/Extraction CTI/)).toBeInTheDocument();
  expect(
    screen.getByText(/réutilisée · depuis un calcul précédent · 1 artifact/),
  ).toBeInTheDocument();
});
