# 2026-11-12 — prune missing backlog records

David requested a GUI action covering every category in hive4459. Codex owns
implementation; Claude owns independent review. Branch starts at
`9b27caa25deffe9e38447236208eb1cf8bc11c03`. Claude's parallel note was only a
plan and was withdrawn; no duplicate implementation was started.

## Behavior

Book queue now offers **Prune missing files**. It previews every library and
status regardless of filters, counts by status, up to20 example paths,
unverified entries and unavailable roots. Confirm removes records only. Existing
files are never changed or deleted. Found/Eligible remain historical scan totals;
the current book counts refresh. Renames are not reconciled by this feature.

Pause crawl/queue and wait for workers to settle first. Both the manager's
thread guard and database readiness checks refuse active work. Absence requires
a real not-found stat error under a readable root; permission and Windows
network-path errors remain unknown. Roots are checked before and after walks.

The local-only POST `/api/library-backlog/books/prune-missing` defaults to
`{"dry_run":true}`. Apply requires `dry_run:false`, the preview's integer
`expected_count`, and its `snapshot_digest`. The digest binds the queue-row
snapshot and findings. Apply repeats discovery, takes BEGIN IMMEDIATE, verifies
the row snapshot/readiness again, then rechecks every candidate and its roots
before deleting anything. A restored file, unavailable root or changed queue
invalidates confirmation and requires another preview.

Deletion is one transaction. Prior event rows survive with book_id detached;
each removed record gets an audit note with original ID/path/state. Related
stages/phases cascade. Scan summaries and files remain unchanged. No live
database or library operation has been performed by this implementation.

## Verification and limits

- **17 new tests** cover all8 nonactive categories, preview immutability,
  retained file bytes, scan history, event/FK integrity, unavailable roots,
  permission/network failures (including empty error messages), restored files,
  snapshot/count validation, running queue/crawl/jobs, unsettled threads, late
  queue changes, transient root outages, transactional rollback, cross-connection
  writer exclusion and local-only API confirmation.
- All available non-model unit modules: **122 passed in20.547s** with UTF-8
  stdout and an isolated temporary directory/processing lock. Unchanged base
  under the same configuration: **105 passed in19.549s**, a delta of17 tests.
  An earlier final-code run also passed122 before the empty-message subcases.
- The entire model suite is not claimed green on this host. Initial full
  discovery at candidate/base produced the same3 unavailable-module imports
  (numpy, cv2, torch) and the same cp1252 stdout failure. UTF-8 stdout resolves
  that output issue. A later run hit the existing shared-default processing
  lock; it refused and no model work ran. Final checks use a private temp root.
  The missing GPU/model dependencies were not installed for this backlog change.
- Frontend `npm ci --ignore-scripts` and `npm run build` passed (Node24.19.0,
  npm11.17.0). A headless browser checked the built page using an intercepted
  fixture API: preview text/counts and unavailable-root notice, Cancel with no
  apply call, Confirm carrying the count/digest, success refresh, no page errors.
  The preview screenshot was visually inspected. All browser API calls were
  intercepted; no live Camelia API or data was used.
- New CI explicitly checks the backlog, queue editor and local API tests on
  Windows/Python3.11 plus a frontend build on Node22. It is scoped to these
  components, not a substitute for the unavailable model-suite tests.

Thirteen in-memory guard mutations were caught on the final source:

| Bypass | Failing new tests |
| --- | ---: |
| Accept Windows network-path failure as absence | 1 |
| Ignore preview count/digest match | 5 |
| Ignore late database snapshot changes | 1 |
| Skip final file checks | 3 |
| Remove BEGIN IMMEDIATE | 1 |
| Skip pre-stat root check | 1 |
| Skip post-stat root check | 1 |
| Skip final database readiness check | 1 |
| Assume roots readable | 3 |
| Ignore active database work | 2 |
| Treat empty file errors as success | 1 |
| Hide empty root errors | 1 |
| Bypass manager worker guard | 1 |

Sources were not edited for mutation tests. SHA-256:
backlog_prune.py `bffb5b89d79c09196e9b08ca868898143180dca0129e66025823013eff842037`;
tests `532d01696cb2b6ad0fd0f3738bfe2ead3c1ac55821eb609a8848c55116ad615e`.
External logs/scripts/screenshot use `camelia-prune-*`,
`camelia_prune_ui_check.py`, `camelia_core_checks.py` and
`mutate_camelia_prune.py` under Codex scratch worktrees. The source/code commits
are eight one-file commits through `4ab6eb91616c6ea86c36edc9cb05c8c60d9c2668`;
this log is the ninth commit and changes no executable behavior.

Rollout is pending independent exact-head review and both CI jobs. Restarting
the actual Camelia backend/frontend remains with David's owning interactive
session; this work did not deploy or invoke the live prune endpoint.
