# Contributing

Thanks for looking. The project is small on purpose; contributions that keep it
small are the easiest to merge.

## Setup

```
git clone https://github.com/KatsaounisThanasis/cloudmap && cd cloudmap
pip install -e ".[dev]"
pytest && ruff check .
```

Python 3.9+ (CI runs 3.9 and 3.13). No test touches a real cloud: `subprocess`
is faked everywhere, and several test files have an autouse fixture that turns
any real subprocess call into a failure - keep it that way.

## The rules that are not up for debate

These are the project's identity; PRs that violate them will be declined even
if the feature is useful:

1. **Read-only.** No `az` command that mutates anything.
2. **Local-first.** No telemetry, no upload, no outbound call except the
   optional user-configured local model endpoint.
3. **Every edge carries evidence.** An edge without a provable source
   (property path, config reference, RBAC assignment) does not go on the map.
4. **A model proposes, code verifies.** LLM output may only suggest candidates
   that a deterministic check confirms against scanned resources. A guess that
   cannot be verified is dropped, not shown.
5. **Incompleteness is declared.** If a scan was truncated or a read failed,
   the artifact says so. Never let an empty result pass for "nothing depends
   on this".
6. **Nothing sensitive in the repo.** Fixtures are synthetic or scrubbed;
   `tests/test_fixtures_safe.py` enforces it in CI.

## Practical notes

- Match the existing style: docstrings explain *why*, tests are named as
  behaviour claims (`test_a_truncated_capture_stays_truncated_when_retraced`).
- A bug fix needs a regression test that fails without the fix.
- New resource-type support usually means: a Resolver index entry, an
  extractor rule (or nothing, if the generic ARM-reference pass covers it),
  a FRIENDLY name, and a fixture-based test.
- Run `ruff check .` before pushing - CI treats lint as a failure.

## Reporting bugs

Open an issue with the command you ran and the output. If the map itself is
wrong (missing or bogus edge), the perfect report includes a minimal synthetic
fixture that reproduces it - see `fixtures/contoso.json` for the shape. Never
paste a real capture; scrub it first (`cloudmap scrub`) and read it before
posting.

Security issues: see [SECURITY.md](SECURITY.md) - do not open a public issue.
