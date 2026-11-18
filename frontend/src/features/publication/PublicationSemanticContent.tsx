import type { PublicationDocument } from "../../api/production";

export function PublicationSemanticContent({
  document,
  text,
  anchor,
}: {
  document: PublicationDocument;
  text: string;
  anchor: string;
}) {
  if (document.schema_version === "4") return text;
  const paragraph = document.rich_text.paragraphs.find(
    (item) => item.anchor === anchor,
  );
  if (
    !paragraph ||
    paragraph.spans.map((span) => span.text).join("") !== text
  ) {
    return text;
  }
  return paragraph.spans.map((span, index) => {
    const key = `${anchor}-${index}`;
    switch (span.role) {
      case "actor":
      case "campaign":
      case "malware":
      case "tool":
      case "product":
        return (
          <strong
            className={`semantic-role semantic-role--${span.role}`}
            key={key}
          >
            {span.text}
          </strong>
        );
      case "english_term":
        return (
          <em className="semantic-role semantic-role--english-term" key={key}>
            {span.text}
          </em>
        );
      case "technical":
        return (
          <span className="semantic-role semantic-role--technical" key={key}>
            {span.text}
          </span>
        );
      case "technical_literal":
      case "ioc":
      case "path":
      case "command":
      case "protocol_field":
        return (
          <code
            className={`semantic-role semantic-role--${span.role}`}
            key={key}
          >
            {span.text}
          </code>
        );
      case "source":
      case "proof":
        return (
          <span
            className={`semantic-role semantic-role--${span.role}`}
            key={key}
          >
            {span.text}
          </span>
        );
      case "text":
        return <span key={key}>{span.text}</span>;
    }
  });
}
