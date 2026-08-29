import { ProductionArtifactView } from "./components/ProductionArtifactView";
import { SubjectWorkbench } from "./components/SubjectWorkbench";
import { EditionCreatePage } from "./pages/EditionCreatePage";
import { EditionDetailPage } from "./pages/EditionDetailPage";
import { EditionListPage } from "./pages/EditionListPage";
import { Link, usePathname } from "./routing";

export function App() {
  const pathname = usePathname();
  const detail = pathname.match(/^\/editions\/([^/]+)$/);
  const subject = pathname.match(/^\/subjects\/([^/]+)$/);
  const artifact = pathname.match(
    /^\/subjects\/([^/]+)\/production\/artifacts\/(references|extraction|synthesis|publication)$/,
  );
  return (
    <main>
      <header className="app-header">
        <Link to="/editions">CTI Bulletin</Link>
        <span>Utilisateur local : dev-analyst</span>
      </header>
      {artifact ? (
        <ProductionArtifactView
          subjectId={artifact[1]!}
          stage={
            artifact[2] as
              "references" | "extraction" | "synthesis" | "publication"
          }
          onClose={() => window.history.back()}
        />
      ) : subject ? (
        <SubjectWorkbench subjectId={subject[1]!} />
      ) : pathname === "/editions/new" ? (
        <EditionCreatePage />
      ) : detail ? (
        <EditionDetailPage editionId={detail[1]!} />
      ) : (
        <EditionListPage />
      )}
    </main>
  );
}
