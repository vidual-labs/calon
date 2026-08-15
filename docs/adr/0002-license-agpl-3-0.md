# 2. License calon under the GNU AGPL-3.0-or-later

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

calon is an open-source, self-hostable web service. The license had to be chosen before the
first substantive commit, while the contributor set is one person and relicensing is still
practical.

The relevant property of this project is that **it is a network service, not a library**.
That makes ordinary copyleft weaker than it looks: the GPL's obligations trigger on
*distribution*, and a hosted service is never distributed. A vendor could take calon, run it
as a paid booking SaaS, improve it substantially, and owe nothing back to anyone.

The candidates considered were AGPL-3.0-or-later, Apache-2.0, and MIT.

## Decision

calon is licensed under the **GNU Affero General Public License, version 3 or later**.

The deciding factor is **AGPL section 13, "Remote Network Interaction"**, which extends the
source-offering obligation to users who interact with the software *over a network*. For a
self-hosted web tool, this is the only clause that makes copyleft mean anything at all.

Three supporting reasons:

1. **It costs honest self-hosters nothing.** Running unmodified calon for your own bookings
   triggers no obligation. Modifying it for internal use creates an obligation only to the
   users you actually serve, not to the public.
2. **The clean API boundary makes it low-friction.** calon is a standalone service that
   integrators talk to over HTTP. A separate program communicating with calon across an API
   is not a derivative work, so the AGPL does not reach into proprietary callers — including
   external lead sources such as OpenFlow. The standalone-first architecture and this
   license reinforce each other.
3. It preserves the norm of contributing improvements back without needing a CLA or a
   dual-licensing apparatus.

The full FSF text is shipped **verbatim** as `LICENSE`. The license document itself forbids
modification, so the project's copyright notice lives in `README.md` and in source headers
rather than being edited into the license text. `pyproject.toml` carries the SPDX
identifier `AGPL-3.0-or-later`.

## Consequences

- Anyone offering a modified calon as a hosted service must offer its users that modified
  source.
- **Adoption cost, accepted knowingly:** the AGPL appears on many companies' internal
  dependency deny-lists. Some organisations will not use calon, and some will not contribute
  to it. For a self-hosted tool aimed at small operators, reciprocity is worth more than
  vendor embedding.
- Integrating with calon over its HTTP API is unaffected, which is the integration path the
  architecture is built around anyway.
- Relicensing later requires the agreement of every contributor. If permissive licensing
  ever matters more than reciprocity, that decision gets meaningfully harder with each
  contributor — Apache-2.0 would be the alternative, for its explicit patent grant.
- Contributions are accepted under the same license. There is no CLA.
