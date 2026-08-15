# Security policy

## Supported versions

calon is pre-alpha and has **no released version yet**. Once `0.1.0` ships, security fixes
will be applied to the latest minor release only, in keeping with its pre-1.0 status.

| Version | Supported |
| --- | --- |
| `main` (unreleased) | ✅ |

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through
[GitHub Security Advisories](https://github.com/vidual-labs/calon/security/advisories/new).
If that is unavailable to you, open a public issue containing only the words "security
report, please make contact" and no technical detail, and a maintainer will follow up.

Please include, as far as you can:

- the affected version or commit
- a description of the issue and its impact
- reproduction steps or a proof of concept
- any suggested remediation

You can expect an acknowledgement within 7 days and an assessment within 30 days. We ask
for a 90-day disclosure window, or less by mutual agreement once a fix is available.
Reporters are credited in the changelog unless they ask not to be.

## Scope notes for self-hosters

calon is designed to be self-hosted, and several security properties are the operator's
responsibility rather than the application's:

- **Run calon behind TLS.** It does not terminate TLS itself.
- **The booking form is intended to be public; the API is not.** Restrict `/api/v1/…` at
  your reverse proxy if you do not need it exposed.
- **Keep `config/calon.toml` out of version control.** It may contain per-source shared
  secrets. It is git-ignored by default.
- **Back up the SQLite database file.** It is the entirety of your booking state.
- Booking data is personal data. Consider your retention obligations before enabling long
  audit history.
