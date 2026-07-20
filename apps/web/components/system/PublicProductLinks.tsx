import { publicProductLinks } from "../../lib/public-config";

type PublicProductLinksProps = {
  className?: string;
  discloseSessionCookie?: boolean;
};

const links = [
  ["Terms", publicProductLinks.terms],
  ["Privacy", publicProductLinks.privacy],
  ["Support", publicProductLinks.support],
  ["Service status", publicProductLinks.status],
] as const;

export function PublicProductLinks({ className = "", discloseSessionCookie = false }: PublicProductLinksProps) {
  return (
    <footer className={`text-center text-[12px] leading-5 ${className}`} style={{ color: "var(--text-dim)" }}>
      {discloseSessionCookie ? (
        <p className="mx-auto mb-2 max-w-lg">
          Chronos uses an essential, secure session cookie to sign you in. It does not use advertising cookies.
        </p>
      ) : null}
      <nav aria-label="Legal and support" className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
        {links.map(([label, href]) => href ? (
          <a
            key={label}
            href={href}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-transparent underline-offset-2 hover:decoration-current"
          >
            {label}
          </a>
        ) : null)}
      </nav>
    </footer>
  );
}
