import { navigate } from "./navigation";

export function Link({
  to,
  children,
}: {
  to: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={to}
      onClick={(event) => {
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
