# Chronos Desktop for macOS

Chronos Desktop is the least-privilege local bridge for approved Chronos work. The web app issues short-lived, HMAC-signed commands; the native client validates the signature, expiry, device binding, nonce, command allowlist, and folder grant before it performs anything locally.

> **Distribution status:** source, backend integration tests, the Swift self-test,
> and local ad-hoc DMG packaging exist. An ad-hoc DMG is not a client release.
> Do not follow the install steps below for real clients until the protected
> `desktop-production` GitHub environment has built a Developer ID-signed,
> notarized, stapled release and that exact artifact has been installed and
> exercised on a clean supported Mac.

## Install and pair

1. Install the notarized `Chronos Desktop.app` in `/Applications`.
2. In the Chronos web app, open **Settings → Desktop devices** and create a single-use pairing code.
3. Open Chronos Desktop, confirm the API URL, enter the pairing code, and choose **Pair securely**.
4. Authorize only the folders required for local work. Absolute paths and security-scoped bookmarks remain on the Mac; the server receives an opaque grant identifier and display name.

The menu-bar icon shows connection state. `Option + Space` opens the app. Disconnecting erases the device token, command secret, cached results, and every local folder authorization, while also revoking the server-side device when it is reachable.

## Security model

- Device tokens and command secrets live in macOS Keychain.
- Folder access uses security-scoped bookmarks and rejects symlink escapes.
- Commands are structured JSON. Arbitrary shell strings are never accepted.
- Executables, arguments, runtime, working directory, and output size are allowlisted and capped.
- Command nonces are replay-protected and results are signed before submission.
- Failed result delivery is encrypted in Keychain and retried idempotently.
- Production API URLs must use HTTPS. Plain HTTP is accepted only for loopback development.
- The App Sandbox permits only outbound networking and user-selected folder access.

## Develop and verify

```bash
cd apps/desktop-macos
swift build
swift run ChronosDesktop --self-test
CHRONOS_CODESIGN_IDENTITY=- scripts/package-dmg.sh
codesign --verify --deep --strict --verbose=2 "dist/Chronos Desktop.app"
```

The ad-hoc signature is for local verification only. Client distribution must use a Developer ID Application certificate and Apple notarization:

```bash
export CHRONOS_CODESIGN_IDENTITY="Developer ID Application: Cognisia (…)"
export APPLE_ID="release-owner@example.com"
export APPLE_APP_PASSWORD="…"
export APPLE_TEAM_ID="…"
scripts/package-dmg.sh
scripts/notarize.sh
```

The `desktop-release.yml` workflow performs the same signed, notarized release
on `desktop-v*` tags. It accepts only a commit on `main` with successful CI and
an approved merged pull request, runs the Swift security self-test, then checks
the checksum, DMG structure/signature/staple, read-only mount, app signature,
Gatekeeper assessment, bundle versions, and the packaged executable's
`--self-test` before upload or publication. Manual runs must target the current
`main` head. Configure its protected `desktop-production` environment with the
secrets documented in the workflow before publishing.

Release evidence must include the workflow/tag/commit, DMG SHA-256, Developer ID
signature verification, notarization/stapling result, Gatekeeper launch on a
clean Mac, one-time pairing, notification permission, folder grant, approved
read/command/app-open result, offline result retry, grant/device revocation, and
confirmation that the server never receives an absolute local path.
