import { PublicProductLinks } from "../components/system/PublicProductLinks";

export default function NotFound() {
  return (
    <main className="flex min-h-[100dvh] items-center justify-center px-5" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <section className="surface w-full max-w-lg rounded-2xl border border-soft p-6 text-center sm:p-8">
        <div className="text-[12px] font-semibold uppercase tracking-[0.18em]" style={{ color: "var(--accent)" }}>404</div>
        <h1 className="h-section mt-3">That page is not in this workspace</h1>
        <p className="mt-2 text-[13.5px] leading-6" style={{ color: "var(--text-muted)" }}>The link may be outdated, or your role may not have access to the resource.</p>
        <a className="btn btn-accent mt-6 inline-flex justify-center" href="/chat">Return to Chronos</a>
        <PublicProductLinks className="mt-6" />
      </section>
    </main>
  );
}
