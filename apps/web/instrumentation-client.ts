import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

Sentry.init({
  dsn,
  enabled: Boolean(dsn),
  environment: process.env.NEXT_PUBLIC_CHRONOS_ENVIRONMENT || process.env.NODE_ENV,
  release: process.env.NEXT_PUBLIC_CHRONOS_RELEASE,
  sendDefaultPii: false,
  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.05 : 0,
  beforeSend(event) {
    if (event.request) {
      delete event.request.cookies;
      delete event.request.data;
      delete event.request.query_string;
      if (event.request.url) event.request.url = event.request.url.split("?", 1)[0];
      for (const key of Object.keys(event.request.headers || {})) {
        if (["authorization", "cookie", "x-csrf-token"].includes(key.toLowerCase())) {
          event.request.headers![key] = "[Filtered]";
        }
      }
    }
    return event;
  },
  beforeBreadcrumb(breadcrumb) {
    if (breadcrumb.category === "console") return null;
    if (typeof breadcrumb.data?.url === "string") {
      breadcrumb.data.url = breadcrumb.data.url.split("?", 1)[0];
    }
    return breadcrumb;
  },
});

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
