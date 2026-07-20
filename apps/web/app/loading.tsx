export default function AppLoading() {
  return (
    <main className="flex min-h-[100dvh] items-center justify-center px-5" style={{ background: "var(--bg)", color: "var(--text)" }} aria-busy="true" aria-live="polite">
      <div className="text-center">
        <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl text-lg font-semibold" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif" }}>C</div>
        <p className="mt-4 text-[13px]" style={{ color: "var(--text-dim)" }}>Opening your workspace…</p>
      </div>
    </main>
  );
}
