import { SubjectProduction } from "../../components/SubjectProduction";

export function PipelineTab({ subjectId }: { subjectId: string }) {
  return <SubjectProduction subjectId={subjectId} />;
}
