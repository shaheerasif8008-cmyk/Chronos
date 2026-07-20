import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTypeScript,
  {
    rules: {
      // Chronos loads API state and starts polling from effects. The React 19
      // compiler-oriented rule rejects that established React 18 pattern even
      // when the state transition occurs inside an async loader.
      "react-hooks/set-state-in-effect": "off",
      // Artifact previews use authenticated blob/data URLs that Next/Image
      // cannot optimize or safely proxy.
      "@next/next/no-img-element": "off",
    },
  },
  globalIgnores([
    ".next/**",
    ".next-mobile/**",
    "node_modules/**",
    "test-results/**",
    "playwright-report/**",
    "next-env.d.ts",
  ]),
]);
