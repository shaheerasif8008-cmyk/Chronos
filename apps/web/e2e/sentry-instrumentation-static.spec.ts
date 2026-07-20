import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const read = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("Next.js error monitoring covers client server edge and render boundaries", () => {
  const client = read("instrumentation-client.ts");
  const server = read("sentry.server.config.ts");
  const edge = read("sentry.edge.config.ts");
  const instrumentation = read("instrumentation.ts");
  const routeError = read("app/error.tsx");
  const globalError = read("app/global-error.tsx");

  for (const config of [client, server, edge]) {
    expect(config).toContain("sendDefaultPii: false");
    expect(config).toContain("delete event.request.cookies");
    expect(config).toContain("delete event.request.data");
    expect(config).toContain("delete event.request.query_string");
    expect(config).toContain('key.toLowerCase()');
    expect(config).toContain('breadcrumb.category === "console"');
    expect(config).toContain("NEXT_PUBLIC_CHRONOS_RELEASE");
  }
  expect(client).toContain("captureRouterTransitionStart");
  expect(instrumentation).toContain("captureRequestError");
  expect(instrumentation).toContain('NEXT_RUNTIME === "nodejs"');
  expect(instrumentation).toContain('NEXT_RUNTIME === "edge"');
  expect(routeError).toContain("Sentry.captureException(error)");
  expect(globalError).toContain("Sentry.captureException(error)");
});

test("production carries the DSN as runtime and build secrets without a build argument", () => {
  const dockerfile = read("Dockerfile");
  const nextConfig = read("next.config.mjs");
  const workflow = read("../../.github/workflows/deploy-aws.yml");
  const terraform = read("../../infra/ecs.tf");

  expect(dockerfile).toContain("--mount=type=secret,id=sentry_dsn");
  expect(dockerfile).not.toContain("ARG NEXT_PUBLIC_SENTRY_DSN");
  expect(workflow).toContain("secret-envs:");
  expect(workflow).toContain("sentry_dsn=WEB_SENTRY_DSN");
  expect(workflow).toContain('echo "::add-mask::$WEB_SENTRY_DSN"');
  expect(terraform).toContain('{\n      name      = "SENTRY_DSN"');
  expect(nextConfig).toContain("NEXT_PUBLIC_SENTRY_DSN");
  expect(nextConfig).toContain("sentryOrigin");
});
