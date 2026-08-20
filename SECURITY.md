# Security

cloudmap reads cloud infrastructure and handles the output, so security reports
get priority over everything else.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/KatsaounisThanasis/cloudmap/security/advisories/new)
(Security tab → Report a vulnerability). Please do not open a public issue for
anything that could expose a user's infrastructure data before a fix exists.

You can expect an acknowledgement within a few days. There is no bounty - this
is a solo open-source project - but reports are credited in the release notes
unless you ask otherwise.

## What counts as a vulnerability here

The interesting failure modes for a tool like this:

- **Credential leakage into artifacts**: any way a secret, key, token or
  password ends up in a `.drawio` / `.json` / `.html` / `.csv` / capture file.
  The scrubber (`cloudmap/scrub.py`) redacts credentials and pseudonymises
  identifiers; a value that survives it is a bug of the highest priority -
  one shipped before (base64 padding, fixed in `2b1e06d`) and the regression
  test suite grew from it.
- **Scope escalation**: cloudmap must only ever read what the caller's own
  `az login` token can read, and must never perform a write operation against
  the tenant.
- **Injection through cloud-controlled data**: resource names, tags and
  properties are attacker-influenceable in shared tenants; anything that lets
  them break out of an `az` argument list, the HTML viewer, or the draw.io XML
  is in scope.

## Design promises you can hold us to

- Read-only: no `az` mutation commands, ever.
- Local-first: the only outbound network call in the codebase is to a
  user-configured local model endpoint (`cloudmap/local_model.py`), off by
  default. Resource data never leaves the machine.
- Secrets stay in memory: `--resolve-secrets` substitutes Key Vault values
  in-memory for edge extraction and never prints or writes them.
- `tests/test_fixtures_safe.py` fails CI if a committed fixture ever carries
  a credential or a real-looking identifier.

If you find code contradicting any of these, that is a valid report even if
you cannot demonstrate an exploit.
