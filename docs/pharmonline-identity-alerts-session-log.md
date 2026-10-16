# Pharmonline identity alerts — session journal

## 2026-10-16 — Scope, routing and initial evidence (Asia/Baku)

- User requested a mailbox review, provided Pharmacy Monitor alert screenshots, then said “начинай” after the proposed next step to inspect run #1055 and conflicting product identities. Scope: diagnosis, a concrete safe correction where supported, targeted verification and checkpoint commits. No push, merge, deployment, database reconciliation or new scrape has been authorized explicitly.
- Mail evidence: run #1046 on October 15 and #1055 on October 16 failed with `PharmonlinePublicAPIIdentityError`; scheduled admission refused `plan_unsafe:native_id_url_rebind_unproven=1`. Run #1055 reported 9,430 API IDs, 10,384 trusted IDs, 7 ID collisions, 8 URL collisions and 1 mismatched URL. The 08:00 Baku email reported no confirmed catalog update for 54 hours. Later run #1056 being `ok` did not resolve the catalog alarm. The alert also renders the failed #1055 as `pharmonline=#1055:ok` in another section.
- Started at `/home/codex-alt/projects/pharmacy-monitor`, local `main` = `177c2e28`. Preserved the untracked `.venv` symlink and unrelated worktrees. Initial `codex-project-context` resolved origin `https://github.com/rashadrahimov/pharmacy-monitor.git`, production alias `pharmacy-monitor-prod`, path `/opt/pharmacy-monitor`, manual workflow dispatch release.
- Initial fetch used the wrong active GitHub account and returned repository-not-found. Per-command credentials for the configured owner `rashadrahimov` successfully fetched `origin/main` = `7dfcf954a909a6a110418de88d9a3daa88029e59`; global account configuration was unchanged.
- Created dedicated worktree `/mnt/HC_Volume_106454338/codex-alt-data/worktrees/pharmacy-monitor-identity-alerts-20261016`, branch `codex/pharmonline-identity-alerts-20261016`, from that current remote main.
- Routing conflict: host contract and `pharmacy-monitor-prod` SSH alias resolve production to `46.225.149.52`; fresh origin/main `.github/workflows/deploy.yml` instead uses `13.140.186.143`. Earlier local workflow was stale. No production changes made. One read-only SSH attempt to the contract address timed out. Asked user which production address is current, per explicit AGENTS instruction. All further production access remains pending that answer.
- Continue independently: review identity proof/reconciliation and health reporting code, reproduce supported defects in targeted offline tests, preserve identity safety guards. No assumption that a proxy failure caused this incident.

## 2026-10-16 — Routing resolved; offline diagnosis and regression

- User explicitly confirmed `13.140.186.143` as the current production server. This supersedes the old `46.225.149.52` address for this task. Resume read-only inspection using the dedicated Pharmacy Monitor SSH identity and a per-command hostname override; do not change global SSH configuration.
- Independent identity review found no proven unsafe-guard defect from the mail alone. Seven missing trusted IDs can themselves explain seven ID collisions; these counters do not mean seven duplicate products. One changed URL remains unproven, intentionally blocking atomic reconciliation. Preserve existing barcode/name, two-observation redirect and quarantined-history requirements.
- Fresh origin/main contains neither `scheduled_admission` nor `plan_unsafe`, although both occur in the live alert. Establish deployed code provenance before any source-level recovery proposal.
- Confirmed reporting defect: full-catalog summary prefers per-site scrape `ok` over the overall failed attempt and omits that attempt's error after later partial success. Added four deterministic regression cases (error field, catalog-reason field, quality-reason fallback, missing reason), including verified full recovery. All four fail against the original implementation as expected.
- Existing canonical `.venv` symlink points to a missing directory; preserved it. Built an isolated worktree venv without ensurepip, then installed only locked pytest/pytest-asyncio, SQLAlchemy, structlog and ruff plus their small dependencies. Python is 3.12 on this host; CI uses 3.11. No full dependency sync or browser install.
- Local resource checks: ~15 GiB RAM available, 234 GiB disk free, memory PSI avg10/60/300 = 0. Targeted checks run sequentially.

## 2026-10-16 — Reporting correction verified; access investigation continues

- Corrected full-catalog health summary to display the terminal run status and separately label a differing scrape status. Preserved the failed attempt's error and catalog verification reason (including quality-envelope fallback) after later partial success. No identity, financial eligibility, severity, persistence or reconciliation rules changed.
- Verification: four regressions failed against the original implementation; after the correction, all 45 tests in `tests/test_health.py` pass. Added mixed-site coverage so a failed site stays visible even when the overall run is recorded `ok`. Targeted ruff check and diff whitespace check pass. Root reviewed the implementation diff independently of the implementing agent.
- Full Python suite / frontend build / browser E2E: NOT RUN; prohibited heavy verification on Contabo and unnecessary for this localized reporting correction. GitHub CI / required agent-review: NOT RUN yet; no push or PR publication authorized.
- Actual target accepted connections but rejected the old dedicated key for both root and pm. User said to investigate existing access independently. Read-only inspection of Contabo's existing SSH metadata found a separately named Pharmacy CI key dated October 2026 in the owner's account. It is used in place, without copying or exposing private key material.
- Git history confirms migration to `13.140.186.143` in `b3913119` on September 3; old server drained. Latest GitHub main is still September 7, and latest workflow run found is September 8. This does not establish the current deployed code provenance.

## 2026-10-16 — Production access and exact source established

- Read-only production SSH now works through the established Contabo administrative loopback, executing SSH as the owner with the existing October Pharmacy CI identity, user `pm`, target `13.140.186.143`. Private key material never copied or printed. `pm` cannot read the system journal; inspected app files and PostgreSQL through explicit `default_transaction_read_only=on` and a bounded statement timeout instead.
- Database confirms #1055 is failed/full/unverified with the mailed identity error. #1056–1060 are successful **partial aloe** diagnostics, not Pharmonline recovery. Last verified full Pharmonline run #1034 finished 2026-10-13 22:05:46 UTC (October 14, 02:05 Baku). Aloe and Aptekonline full runs #1053/#1054 succeeded overnight.
- Source provenance supersedes the earlier stale-repository assumption: owner's existing checkout points at `https://github.com/rashadoni/pharmacy-monitor.git`. Its published current main is `af2ab19b4b5a49649a29f7a416ab6bbb07ca5b1c` (October 14, PR #105). Fetched this as a task-owned local ref without changing the configured origin or either owner's checkout.
- **Exact byte agreement** between that published revision and production: `src/main.py` SHA256 `6c7dcff710f2eeaae5ecf346c4abbec71b22af79900a451cbb151c56cecfee21`; `src/health.py` `f7a80a134062e73a17ceaefad5f22310e69fd7b9c5a1bd7da4f07e708e28f744`; public API scraper `565b95c1fd0f2808f54639f53ddde44da0f9a6f29919e90e5a25765ef4d18c4a`. Scheduled admission exists there and matches the incident.
- Merged the proven current source into the dedicated task branch, preserving checkpoint `7f655ba6` and prior history. Health correction merged automatically; revalidation now targets the actual deployed source, not September's stale snapshot.
- Current delivery policy supersedes earlier agent-review note: required CI gates are Ruff, pytest and frontend build; independent review remains mandatory but agent-review workflow was removed. No push, merge to main or deployment performed. Release routing must use the proven active repository and user approval before any publication/release.
- Production capacity: ~6.2 GiB available RAM, 78 GiB disk free, memory pressure 0; Pharmonline service inactive/failed. Started one bounded direct public-catalog read (180-second timeout, one whole-catalog attempt, no paid proxy), followed only by a PostgreSQL-enforced read-only reconciliation diagnosis. This does not invoke `run`, write catalog data, send email or apply a plan.

## 2026-10-16 — Live conflict identified; reporting revalidated

- First live diagnostic read was complete: 9,431 public products, no source errors. Read-only DB plan is unsafe for three identities: one native-ID URL rebind without proof and two `legacy_row_has_native_id` conflicts (a current API identity uses a URL occupied by a different stored native identity). There are 12 new-product admission candidates and one existing-native candidate. No candidate was written.
- The URL rebind is stored Product #67148, **Kalsimiks № 30 ( Kapsulalar)**. Stored and current public names match exactly; the stored barcode is missing. A previous audit exists, so the available recovery path depends on which ledger contains it. Identity proof objects and canonical URL pairs were not serialized or logged.
- The existing manual recovery accepts only its established proxy-based proof transports; a direct source read is diagnostic evidence, not an accepted recovery ledger proof. Prior admission alone permits a first proven continuity reconciliation; prior reconciliation/quarantine imposes additional append-only restrictions. No guard has been weakened.
- Reporting improvement adapted to actual source: preserve the untruncated `run_quality.catalog_verification_reason_full`, preferring full error text first and truncated legacy fields only as a final fallback. All **53** current health tests pass, targeted Ruff passes, diff whitespace passes. An unrelated inherited formatting-only change was reverted after review; no behavior changed after the passing test run. Small FastAPI and RapidFuzz fixture dependencies were installed in the task-only venv from lock-pinned versions; no full dependency sync.
- Second bounded direct read is inspecting the two newly observed native-ID changes and two fresh redirect observations for the URL move in one process. It remains read-only and explicitly labels direct redirect observations as diagnostics, not accepted workflow proof.

## 2026-10-16 — Diagnosis complete and precise continuation point

- Second bounded direct catalog read also completed with 9,431 products and no source errors. No catalog identity pairs were persisted. Current source conflicts:
  - Product #67148, Kalsimiks №30: same native ID and display name, changed URL; stored barcode missing. Two independent direct redirect observations both returned `redirect_target_legacy_product`, not verified continuity. These are diagnostic observations only; no Decodo workflow proof was generated.
  - Product #67123, Fruto Nyanya wheat biscuits 6+, 120 g: a new API ID occupies the existing URL; old ID absent from this current catalog, same display name, stored barcode missing.
  - Product #69407, Raxme Omega-3 150 ml: same form of native-ID change at the existing URL; old ID absent, same display name, stored barcode missing.
- Read-only ledger/history verification: each of the three has exactly one admission, zero reconciliation rows, zero quarantine references, and one stored price snapshot. Offer observations: 7 / 7 / 2 respectively. Therefore this is not disposable empty data, and neither old audit records nor price history may be silently rewritten.
- Existing reconciliation cannot apply this plan: Kalsimiks lacks accepted continuity proof and its prior admission blocks a quarantine split; the other two hit the explicit native-ID replacement guard. The entire plan is atomic. No product IDs/URLs, ledger rows, prices, configuration, services or mail were modified. No repair run, proxy spend, deployment, push or PR was performed.
- Last verified full Pharmonline run remains #1034, finished October 14 at 02:05:46 Baku; age was 64.0 hours at the final DB check. The website itself is accessible; failure is at the identity-trust boundary after a complete source read.
- Independent review concurs: preserving old history while creating separately audited current identities is a possible recovery design, but the current mechanism does not implement these transitions. It needs versioned evidence, retained previous audits, reference/history validation, replay/failure tests and an explicitly reviewed recovery plan. Do not force the existing quarantine/rekey code or manually patch Product IDs/URLs to make the alert disappear.
- Reporting correction is ready on this dedicated branch: checkpoint `7f655ba6`, integration of byte-verified current production source `4cc6ab6d`, full-reason follow-up `4cbf05b2`. Changed application scope relative to active repository main: only `src/health.py`; regression coverage in `tests/test_health.py`. 53 targeted tests pass; Ruff and diff whitespace checks pass.
- NOT RUN: full Python CI suite, PostgreSQL integration gate, frontend build/browser E2E (heavy gates belong to GitHub CI, no publication/release authorized). No migration needed for the reporting correction. Production behavior remains unchanged until a separately authorized release.
- Current result/status: diagnosis established; production catalog recovery remains unresolved; reporting fix prepared and tested.
- Last completed action: read-only verification of all three audit/history records and independent review of existing recovery restrictions.
- Precise stopping point: clean checkpointed task branch; no production changes, no publication; normal reconciliation is unsafe for the observed source changes.
- Next action: obtain scope agreement for an audited recovery extension preserving prior identities/history, prepare and test that concrete recovery plan, then use the active repository `rashadoni/pharmacy-monitor` CI/PR/release process with task-specific publication and deployment approval. Keep the current worktree and checkpoints; do not resume in either dirty owner checkout or stale canonical main.

## 2026-10-16 — Recovery implementation resumed

- User explicitly said “начинай” after clarification, authorizing development of the audited recovery extension described at the previous stopping point. Resume in the existing dedicated worktree and branch. Publication, merge, deployment and production DB application are separate final actions, not yet authorized.
- `codex-project-context` confirms the saved root/branch and stale configured origin. Preserve shared remote configuration; use the previously byte-verified active `rashadoni/pharmacy-monitor` source. Production remains user-confirmed `13.140.186.143:/opt/pharmacy-monitor`, release via manual GitHub Actions.
- Implementation/verification plan: inspect existing quarantine/admission trust validators; design explicit identity retirement plus fresh current identity without transferring history or claiming continuity; test history/audit preservation, replay, drift, rollback and tenant boundaries. Independent agent reviewing design and downstream consumers. No production mutation planned in this phase.
- Production impact and rollback will be documented with the concrete design before any release. Required heavy CI gates remain on the CI runner; bounded targeted tests only on Contabo after resource checks. Manual acceptance after a future authorized recovery: a verified full Pharmonline run, current offers available and overdue alert cleared, old product histories unchanged.

## 2026-10-16 — Audited retirement implemented, first targeted verification

- Implemented an additive `PharmonlinePublicAPIIdentityRetirement` ledger and empty migration `0025_identity_retirements`. Previous admission uniqueness remains unchanged. New replacement identities are authenticated by the retirement ledger itself; original admission, all historical rows and original Product primary key remain intact. Only the archive's external ID and current-offer metadata change. New Product starts without history or canonical matching.
- Narrow v1 handles exactly the two diagnosed forms: same native ID/new URL with two established self-redirect observations, and replacement native ID at the same URL with old ID absent from the complete source. Requires one valid prior admission, strict name agreement, no prior transition graph and globally unambiguous ownership. Old and new identities are separated, never asserted continuous.
- Manual opt-in added to plan/apply and both workflows, default false. Scheduled admission cannot invoke it. Plan v4 binds the exact retirement actions and a digest of every original admission field. Apply requires matching catalog/plan proof, Decodo, migrated schema, fresh locked Product state and refreshed audit rows. Normal persistence verifier validates the new ledger, rejects tampering/orphan markers and excludes archived DDP history only with a valid ledger.
- Independent review discovered shadow-policy consumers could still use archive prices. Agent implemented unconditional exclusions in offer/financial policy, matching, manual suggestions and ROI, including SQL projections; root reviewed commit `0b4e8ec3`. 26 targeted policy/eligibility tests passed.
- Core targeted run: 123 tests passed, 2 PostgreSQL-specific tests skipped (no test PostgreSQL service configured on Contabo). Includes the three-card batch, exact history/admission preservation, no-write defaults, source/audit drift, tampering, replay and rollback after an injected partial failure.
- Delivery targeted run: 28 tests passed. Actually executed both workflow Python validators with valid/invalid v4 artifacts, parsed all changed shell steps without execution, compared migration column/FK/uniqueness shape with model, verified empty migration reversibility and refusal to drop used evidence; existing retry/lock tests pass.
- Only lock-pinned small test dependencies installed in task venv (no full environment sync or browser binaries). Resource checks before gates: ~15 GiB available RAM, 232 GiB disk, memory pressure 0. Checks sequential.
- Independent review found and root corrected missing v4 evidence key in plan workflow and explicit missing-schema refusal. All source manifests stage whole tracked `src`, so new module travels on supported release/recovery routes. Selective legacy health hotfix workflow is not a supported route for this change.
- Added `docs/pharmonline-identity-recovery.md` with concrete release, plan, apply, acceptance and rollback steps. No automatic DB rollback is claimed: recovery and later full scrape are separate transactions; workflow retains its backup on failure. Used retirement evidence cannot safely be dropped or consumed by old code.

## 2026-10-16 — Final independent review and adjacent-consumer checks

- Independent reviewer added checkpoint `01b35fbd`: 11 guards pass covering missing migration, original-manifest replay refusal, second-transition refusal, duplicate source identity/URL, cross-tenant target, stale ORM/raw database changes and preservation of all price/offer/stock/supplier/canonical references. Replacement rows inherit none of those references.
- Final independent review of the complete core and workflow diff found no remaining blocking issue for this narrow three-identity recovery. Both previously reported issues are resolved. Reviewer explicitly distinguishes SQLite stale-state tests from PostgreSQL serializability verification.
- Adjacent comparison projection, match actions/rollback and health tests: 87 passed (one existing datetime.utcnow deprecation warning). Across the sequential targeted gates in this implementation phase: **275 passed, 2 skipped**. Final targeted Ruff and diff whitespace checks pass. Formatting limited to task-owned new model/modified recovery functions; no unrelated whole-file rewrite.
- NOT RUN: required full GitHub Python/Frontend CI, PostgreSQL concurrency and migration-parity integration gates, browser E2E. No configured isolated PostgreSQL test service here; heavy/full verification belongs to CI, and no publication/deployment was authorized during this implementation phase. Source code, migration and workflow tests are ready for the release route in the recovery document.
- Production remains unchanged. Live Decodo plan and recovery have NOT RUN: two fresh full source passes and redirect observations still must confirm the current live situation after deployment. Tests establish implementation behavior, not that the production catalog is already recovered.

## 2026-10-16 — Implementation checkpoint and stopping point

- Core implementation, migration, manual workflows, regression tests and release/rollback runbook committed as `fa2b4bee`; downstream exclusion and independent guard tests are checkpoints `0b4e8ec3` and `01b35fbd`. Dedicated branch retained; task worktree clean after checkpoint.
- Current result/status: audited recovery implementation complete and independently reviewed; 275 targeted tests passed, 2 PostgreSQL tests skipped; production recovery still pending.
- Last completed action: saved the reviewed implementation and concrete release/recovery procedure in checkpoint commits, checked targeted lint and whitespace.
- Precise stopping point: no push, PR, merge, deployment, production migration or recovery application. Required full CI/PostgreSQL gates and fresh live Decodo proof remain outstanding.
- Next action: publish this reviewed branch to active `rashadoni/pharmacy-monitor` as a PR and run required CI with task-specific authorization; then show the owner the concrete visible-change list before merge/deploy, and obtain/review the live read-only recovery plan before applying it. Do not use the stale configured origin, retired server, or selective health-hotfix route.

## 2026-10-16 — PR publication and full CI authorized

- User replied “начинай” to the explicit request to publish the PR and run full CI. This authorizes branch publication, PR creation and CI/fixes needed for those checks; it does not authorize merge, deployment or applying the production recovery plan.
- Resumed at the saved worktree/branch. Clean status, latest checkpoint `d11fee96`. Active repository `rashadoni/pharmacy-monitor` main remains `af2ab19b4b5a49649a29f7a416ab6bbb07ca5b1c`; no PR exists for this branch. Stale shared `origin` remains untouched; publish with an explicit active-repository URL.
- Required CI runs automatically on the PR: Ruff plus new-file formatting; full pytest with PostgreSQL 16, Redis and Chromium, including migrations; frontend typecheck, unit tests and build. No production workflow is triggered by this PR publication.

## 2026-10-16 — Publication preflight defects corrected

- Published the authorized branch to active `rashadoni/pharmacy-monitor` at `dd440093`; no production action.
- Additional independent publication review found two CI blockers before PR creation: new migration 0025 unconditionally created a table already produced by 0001's current-model bootstrap, and a table-name reference violated the existing model-location guard. Both corrected in `b85c7764` and reviewed by root.
- Migration now accepts an exactly matching pre-existing ledger and refuses an incompatible one; column types/nullability, PK/FK, unique constraints, index and distinct-product check are verified. Repeated upgrade and used-evidence downgrade refusal are covered. Existing model-location rule remains unchanged.
- Targeted delivery tests plus existing model-location and backward-compatible migration guards: 28 passed; targeted Ruff, new-file formatting and whitespace checks pass. Tests execute the real 0001 bootstrap, repeat 0025 and reject damaged schema. PostgreSQL full-chain and frozen-0023 schema parity remain required CI checks.

## 2026-10-16 — PR #107 opened; required CI running

- PR https://github.com/rashadoni/pharmacy-monitor/pull/107 created and attached to this Codex task. Published head `59e9a4dcd816047de6740b7862aec2058236a3de`.
- Automatic required CI run https://github.com/rashadoni/pharmacy-monitor/actions/runs/38918390712 started for that exact head. All three named gates registered: Lint (ruff), Test (pytest), Frontend (Next.js). No duplicate manual dispatch was needed.
- PR description records the behavior, validation, independent review and explicit release/rollback limits. No merge/deployment/recovery authorization inferred from the publication request.
- Precision on PostgreSQL coverage: the new retirement behavior/rollback/stale-state tests use SQLite. Full CI adds real PostgreSQL fresh migrations, frozen-0023 model parity and existing shared locking tests; it does not introduce a new retirement-specific PostgreSQL concurrency race test. Do not conflate those checks in release claims.

## 2026-10-16 — First CI feedback: formatting corrected

- CI run 38918390712 reached full pytest successfully after PostgreSQL migration bootstrap. Lint found formatting-only differences in `scripts/reconcile_pharmonline_public_api_identities.py` and `src/product_policy.py`; repository formatting policy checks all files introduced since its baseline, including these previously existing files.
- Applied the pinned Ruff formatter to those two files. ASTs before/after are identical; targeted Ruff, formatting and whitespace checks pass. No behavior or test expectations were changed. Publish the correction to trigger CI for the final PR head.

## 2026-10-16 — Full CI exercised PostgreSQL; delivery harness updated

- First complete Python CI result for 38918390712: 3,596 passed, 5 failed, 10 skipped, 1 expected failure. All five failures were in `test_prod_db_revision_gate.py`: the sandbox emulated the plan-verification step's environment without the new required `PLAN_RETIRE_IDENTITIES` boolean, so the real shell step refused before reaching the intended migration-revision check.
- Actual workflow already writes this verified boolean from the v4 plan artifact. Preserve that fail-closed production behavior; update only the sandbox's explicit prior-step evidence and exercise both false/true modes, including the actual reconciliation argument.
- PostgreSQL migration bootstrap, frozen-0023 model/migration parity and existing shared PostgreSQL tests passed in this full run. Current 2a7373c5 run 38918713541 has green lint and frontend; its Python source/test behavior is the same as the first run except formatting, so the harness fix is needed before the final head can pass all gates.

- Harness correction committed as `b1b5d827` and independently reviewed by root: production workflows are unchanged, sandbox records actual reconciliation argv, false/true mode is explicit, and existing no-write/refusal/backup-order checks remain. All 70 tests in `test_prod_db_revision_gate.py` pass; Ruff, formatting and whitespace pass. Publishing this correction and journal checkpoint for final CI.

## 2026-10-16 — Final-head CI in progress

- Published final test-harness/journal head `f66e244b900c954b98c7a21b921e46adf59a5b1a`; required CI run https://github.com/rashadoni/pharmacy-monitor/actions/runs/38919660950. Lint, frontend and migration bootstrap passed; full pytest is running.
- Cancelled superseded run 38918713541 after the corrected head started. Its lint/frontend were green, but its old harness was already known to fail from completed run 38918390712; this avoids continuing obsolete work without changing any gate.
- Main protection read-only verification: PR required, admin enforcement enabled, exact required checks Lint (ruff), Test (pytest), Frontend (Next.js). No protection changes made.

## 2026-10-16 — PR #107 ready; all required CI gates passed

- Required CI run https://github.com/rashadoni/pharmacy-monitor/actions/runs/38919660950 completed successfully for published head `f66e244b900c954b98c7a21b921e46adf59a5b1a`. All three protected checks passed: Lint (ruff), Test (pytest), Frontend (Next.js).
- Exact full pytest result: 3,602 passed, 10 skipped, 1 expected failure, 7 warnings, 522.54 seconds. PostgreSQL migration bootstrap, frozen-0023 model/schema parity and existing shared PostgreSQL tests ran in CI. Frontend typecheck, unit tests and build passed. No full local suite or build was run on Contabo, as required by the host contract.
- PR https://github.com/rashadoni/pharmacy-monitor/pull/107 remains open; GitHub reports MERGEABLE / CLEAN for that exact head. PR description updated with final CI evidence and the explicit limit that new retirement behavior tests use SQLite, without a new retirement-specific PostgreSQL race test. Older failed/superseded CI runs do not describe the final head.
- Final independent read-only review by publication_review confirmed the release sequence and accurate validation claims. It made no further changes. No production data, migration, service, deployment, main branch or branch protection was changed in this publication turn.
- This final journal update is a local-only checkpoint. All application, migration, workflow and test changes match the published/tested PR head; only the append-only session journal is ahead. Do not silently publish the journal checkpoint and then treat the previous head's CI as validation of a new published head.
- Current result/status: implementation and PR publication complete, independent review complete, all required CI gates green; production catalog recovery remains pending.
- Last completed action: verified final CI and PR readiness, updated PR validation, and checkpointed the continuity journal.
- Precise stopping point: before merge, deployment, production migration and recovery application; current authorization covered PR publication/CI only.
- Next action: present the owner the three visible changes and obtain merge/deploy authorization under docs/DELIVERY-ARCHITECTURE.md lines 140–145. Then merge via PR, wait for green CI on the resulting main SHA, dispatch normal deploy.yml with apply_migrations=true, and produce a fresh read-only Decodo recovery plan on the same SHA. Review and obtain approval of that concrete plan before applying recovery; code deployment alone does not restore the full catalog.

## 2026-10-16 — Fresh recovery plan requested; release prerequisites resumed

- User replied “начинай план восстановления” to the concrete offer to merge PR #107, deploy the reviewed change and prepare a fresh recovery plan. Proceeding with that prerequisite sequence; this does not authorize applying the recovery plan to product records.
- Saved feature worktree/branch retained. PR head remains `f66e244b900c954b98c7a21b921e46adf59a5b1a`, all three required checks green, MERGEABLE/CLEAN; active main remains `af2ab19b4b5a49649a29f7a416ab6bbb07ca5b1c`. Local journal-only checkpoint `c3a4547c` will not be published as application code.
- Reconciled routing: active `rashadoni/pharmacy-monitor`, user-confirmed `13.140.186.143:/opt/pharmacy-monitor`, normal manual deploy.yml. Stale shared origin/SSH alias remain unchanged and unused for release.
- Independent read-only review confirms that a valid v4 retirement plan requires migration 0025. Running it before deployment would spend two full Decodo reads and then refuse with recovery_schema_migrated=0; do not bypass that guard or migrate manually. Supported order: merge tested PR, green CI on resulting main SHA, normal deploy with verified backup/apply_migrations=true, then PLAN on that SHA.
- The plan uses isolated temporary source, two full Decodo reads and redirect observations under the existing scrape lock; DB transaction is read-only, email disabled. It may use the existing paid proxy allowance. No recovery --apply or full scrape is authorized in this step.

## 2026-10-16 — Reviewed PR merged; release SHA awaiting CI

- PR #107 merged through the normal protected PR API with the exact expected head `f66e244b900c954b98c7a21b921e46adf59a5b1a`; no admin bypass or direct main push. Resulting release SHA is `bf35e70c9e0319d567ae107f30323e05ed031a88`.
- GitHub reports the merged tree `c1475d0bec50d0ef3fb23f4a0abe85abd57e098d`, identical to the previously tested PR tree. Required push CI https://github.com/rashadoni/pharmacy-monitor/actions/runs/38922169355 is running; do not dispatch deploy until it succeeds for this exact SHA.
- Local branch remains dedicated and only carries additional append-only journal checkpoints. Production release will use the immutable merged SHA through deploy.yml, not the local journal head.

- Pre-deployment read-only inspection: production DB remains `0024_aloe_product_numbers`; new retirement table is absent. API/frontend active, Pharmonline scrape service failed/inactive; available RAM 6,520,320 KiB, disk 81,449,056 KiB, memory pressure zero. No production mutation during inspection.
- Baseline target rows remain unchanged from diagnosis in counts: #67148 has 1 price/7 observations/1 admission; #67123 has 1/7/1; #69407 has 1/2/1. Current row SHA256 values (PostgreSQL to_jsonb text): #67148 `6912a705d130f1a7f9c3c3cc03f81a03096db3acfe7fc2ba15f53b00cc2d657a`; #67123 `aa74d489be54daca01c856137632e25a0590e4ab26a7b60843d83f60bd010439`; #69407 `6982f5020518bc6906d5515fe81638c5d53b341fdc48ef46ecf3595276d62455`. Inspection used database-enforced read-only mode and 10-second statement timeout.
- Independent acceptance review: verify the fresh artifact's SHA/source manifest, v4/Decodo/retirement=true, exact count coverage, schema readiness and all 16 unsafe metrics zero. Three retirement candidates alone do not prove their names; obtain a private read-only plan with matching candidate hash before presenting named changes. Include additional new/existing admissions and any baseline creation in the human-readable plan, not just the three retirements.

## 2026-10-16 — Release CI green; normal deployment started

- CI run 38922169355 succeeded for merged SHA `bf35e70c9e0319d567ae107f30323e05ed031a88`: all three required checks green. Full pytest: 3,602 passed, 10 skipped, 1 expected failure, 7 warnings in 432.50 seconds.
- Started normal deploy.yml with apply_migrations=true: https://github.com/rashadoni/pharmacy-monitor/actions/runs/38924134664. Confirmed its resolved head SHA is exactly the approved/CI-verified merge SHA. GitHub rejected a direct raw-SHA dispatch (422/no ref, no run); re-read/checked main before dispatching that branch and verified the resulting run SHA.
- Deployment uses its standard preflight, verified backup, migration-before-code sequence and service health checks. No manual migration, direct server deploy or feature-worktree copy. Recovery application remains unauthorized and will not be invoked while preparing the plan.

## 2026-10-16 — Deployment healthy; fresh v4 plan running

- Normal deployment https://github.com/rashadoni/pharmacy-monitor/actions/runs/38924134664 succeeded for release SHA `bf35e70c9e0319d567ae107f30323e05ed031a88`. Standard verified backup, migration-before-code, source sync, frontend build, API/frontend restarts and health/login checks passed.
- Post-deployment DB-enforced read-only check: revision `0025_identity_retirements`, retirement ledger count 0. All three target Product row hashes exactly match the pre-deployment baseline; prices/observations/admissions remain #67148 1/7/1, #67123 1/7/1, #69407 1/2/1. Deployment did not apply recovery or change these cards.
- Started fresh read-only workflow https://github.com/rashadoni/pharmacy-monitor/actions/runs/38924647083 with confirmation=PLAN and retire_conflicting_identities=true. Confirmed its resolved head is the exact deployed/green-CI SHA. Await the artifact, independently validate all metrics/source hashes, then reproduce the private named plan read-only against its exact manifest.

## 2026-10-16 — Fresh recovery plan accepted; named interpretation in progress

- PLAN run https://github.com/rashadoni/pharmacy-monitor/actions/runs/38924647083 succeeded for deployed SHA `bf35e70c9e0319d567ae107f30323e05ed031a88`. Downloaded evidence to `/tmp/pharmacy-recovery-plan-38924647083` (aggregate data/source hashes only).
- Root verified artifact commit SHA, exact source-manifest bytes and SHA256 `a78c4e944d67e30e2c9bd6573a779225991ee9fabf957643d5bc2d69143d3478`. Plan v4, retirement=true, Decodo; catalog fingerprint `9db43f35802865f9b7b0f00b1d510076760fc3652ddc2a684f8b01516a231cba`; candidate manifest `9fa199bcc0b6bc30dbbd9a5cbc815c79eef23456ef34a333c643f76625229f90`.
- Fresh scope: 9,430 products, all classified; all 16 unsafe metrics zero; recovery_schema_migrated=1, reconciliation_safe=1. Ready actions: 3 identity retirements, 12 new public-product admissions, 1 existing-native admission. Other ready rekeys/rebinds/splits zero; no prior retirement/recovered identities. Existing baseline count 1/floor 9,000, so this plan does not create a first baseline. This supersedes the earlier direct diagnostic count of 9,431; use the fresh artifact only.
- To prove named scope, root reviewed and launched the separate read-only `/tmp/pharmacy-readonly-plan-details.py` helper. It verifies deployed source against the plan manifest, runs the exact published planner with apply=False and two fresh Decodo passes, enforces PostgreSQL default read-only + statement timeout, and releases names only when the full evidence exactly matches. No accepted artifact/production source is modified. Bounded runtime 1,800 seconds; active exec session 54742; result destination `/tmp/pharmacy-recovery-plan-38924647083/named-plan.json`.
- Recovery --apply and the subsequent full persistence scrape have not run. Current stop is waiting for named read-only comparison before showing the owner a concrete application request.

## 2026-10-16 — Named recovery plan verified; awaiting application approval

- Strict read-only named comparison completed successfully (exit 0, status=matched). It re-read two complete Decodo catalogs, repeated actual redirect proofs, verified all deployed source files against the official manifest, and matched the entire aggregate artifact. Publication_review independently confirmed all hashes, metrics, count coverage and the exact named action set.
- Final database-enforced read-only verification: retirement ledger remains empty; original Product row hashes and price/observation/admission counts for all three cards still equal the pre-deployment baseline. No recovery transaction or full persistence scrape was run.
- Planned retirement actions:
  - Product #67123: Fruto Nyanya uşaq  peçenyesi buğdalı 6+ (2889) 120 q — native_id_replaced; preserve the old Product/history and create a separate current Product.
  - Product #67148: Kalsimiks № 30 ( Kapsulalar) — native_url_changed; preserve the old Product/history and create a separate current Product.
  - Product #69407: Raxme omeqa 3  150 ml — native_id_replaced; preserve the old Product/history and create a separate current Product.

- New public products to admit:
  - Boro plus krem dəri üçün (5928) 25 ml ( Krem)
  - Diş sapı Oral-B Pro-Expert 25 m
  - Doctor Don Plus 120 ml
  - Klimbazole kəpək əleyhinə şampun (2921) 150 ml (Rusiya)
  - Lenirab 20 mq № 28 ( Kapsulalar) ( İspaniya)
  - Maqnedoz  № 30
  - Poliparazin 120 ml
  - Saksenda 6mq/ml 3 ml № 3
  - Semavik Nekst 1.0 mq 3 ml ( Şpris-qələm) ( Rusiya)
  - Taylol  100 ml (Şərbət) (Türkiyə)
  - Uşaq qidası Kabrita-çox taxıllı pudinq 6+ ay 100 q (Estoniya)
  - Vitamin  C+Sink4 qr  № 20 (Saşe)
- Existing native identity to admit: Product #57072, Nutri D3 50000 BV 15 ml.

- Exact prospective write effects: archive 3 original identities while preserving their histories; create 15 Product rows (3 replacement cards + 12 newly observed products); create 3 retirement ledger rows and 13 admission rows (12 new-product + 1 existing-native). Existing catalog baseline floor stays 9,000. No additional rekey, URL continuity transfer or legacy quarantine split is planned. The recovery workflow subsequently runs a separate full Pharmonline scrape to persist fresh prices/observations and verify coverage.
- Official evidence remains PLAN run 38924647083, release bf35e70c9e0319d567ae107f30323e05ed031a88, candidate manifest 9fa199bcc0b6bc30dbbd9a5cbc815c79eef23456ef34a333c643f76625229f90. Before any later application, recheck the successful plan/source SHA and do not bypass fresh-source/plan drift checks. If source changes, generate/review a new plan instead of forcing the old one.
- Required CI and normal deployment gates: PASSED. Official read-only PLAN, independent artifact review, strict named comparison and no-apply DB verification: PASSED. Recovery application/full persistence scrape: NOT RUN because this turn authorizes plan preparation; docs/pharmonline-identity-recovery.md requires approval of the concrete fresh plan first.
- Current result/status: recovery code is merged/deployed and healthy; fresh named plan is complete and independently reviewed; production recovery has not been applied.
- Last completed action: confirmed exact named effects against the official artifact, verified original cards/history unchanged, and saved the final plan/journal checkpoint.
- Precise stopping point: before recover-pharmonline-decodo-public-api.yml with confirmation=RECONCILE; ledger count remains 0.
- Next action: obtain owner approval of this concrete plan and the subsequent full scrape; then run the supported recovery workflow on the exact deployed SHA using plan_run_id=38924647083 and retire_conflicting_identities=true, verify backup/fresh proof, and inspect full-catalog verification plus preserved original history afterward.

## 2026-10-16 — Owner challenges recurring manual recovery; prevention analysis

- Owner asks why recovery keeps recurring almost weekly for about two months. This is a correction to the proposed endpoint: a single successful manual repair is not a durable resolution. Do not treat this objection as permission to apply the pending plan.
- Current worktree/branch preserved; this phase is read-only production/mail/history investigation plus append-only documentation. No further deployment, application or scrape is being performed.
- Authoritative live ledgers confirm repeated manual admissions on 2026-08-23 (74), 08-30 (35), 08-31 (2), 09-03 (32), 10-04 (267), 10-08 (30), 10-13 (86); reconciliation entries on 08-30 (2) and 10-13 (1); quarantine entries on 08-23 (3) and 10-04 (1). Retirement ledger remains empty. Mail search independently found August and October Pharmonline failure notices. Do not claim every historical incident had the same cause or involved these same three cards.
- Read-only run-history scan since August 15 matched 99 Pharmonline-tagged runs across nine ISO weeks, including repeated failures and some successful recoveries. Initial classification of failure text is heuristic, not a measured root-cause taxonomy; inspect representative errors before asserting transport versus identity causes.
- Code review confirms the durable gap: scheduled admission handles only plain new/existing-native admissions within its limit; identity transitions remain manual, all-or-nothing verification blocks persistence for the entire source, and retirement v1 refuses repeat/chained transitions. PR #107 provides audited recovery for the current class, not prevention of recurring outages. The earlier recovery plan remains valid as incident containment only, subject to freshness checks; it does not establish reliable unattended operation.

## 2026-10-16 — Recurrence confirmed; durable correction scope

- Live admission ledger contains exactly 8 distinct manual proof run references across 7 dates from August 23 through October 13 (October 8 had two). This corroborates the owner's recurring-repair concern. Existing RUNBOOK lines 1599–1612 already documented these 8 repairs and the October 13 deployment of ordinary-new-product auto-admission.
- Representative historical errors establish more than one failure class: Aug 26 #694 and Oct 11 #1018 were identity refusal with newly untrusted IDs; Sep 8 #775, Sep 14 #805 and Sep 30 #936 explicitly show proxy HTTP 407; Oct 4 #968 says Decodo not configured; Aug 23 #678 failed report template lookup after catalog work. Current #1055 refuses a native-ID URL rebind. Do not describe the whole two-month period as one identical bug or promise that identity retirement fixes transport failures.
- Agreed analytical correction: PR #107 is containment for the present identity transition, not a demonstrated unattended-reliability solution. The owner has not authorized applying the pending manual plan in this turn; do not repeat the application-approval question as a substitute for addressing recurrence.
- Proposed durable behavior (NOT IMPLEMENTED): preserve complete-source evidence separately from per-product/per-observation identity trust. Classify a complete catalog into trusted/admissible/conflicted records. Continue updating 9,427 uncontested identities in the current 9,430-item fixture; isolate the 3 conflicts in an idempotent pending-identity journal with no prior-history transfer. Conflicted current/old offers remain excluded from matching, comparisons, financial recommendations, manual proposals and exports, including shadow mode. Health/UI must report source coverage and isolated count honestly; never relabel an incomplete/unsafe catalog as verified simply to pass a gate.
- A later scoped extension may support versioned source-identity bindings/epochs with append-only evidence and repeated transitions. It must not automatically invoke today's one-shot recovery or infer identity continuity from a shared name. Whether upstream churn reflects SKU republishing or API behavior remains unproven.
- Acceptance for the durable change: current three conflicts do not stop other valid updates; repeated/revised conflicts do not multiply rows or inherit history; untrusted offers never reach financial consumers; incomplete source, cross-tenant collisions and mass drift remain hard publication refusals; real PostgreSQL concurrency/rollback/replay coverage; visible truthful degraded status; several observed scheduled runs after release. Existing whole-run financial eligibility means a local catch/continue is insufficient: trusted data propagation must be changed coherently.
- Current result/status: recurrence is confirmed and its present architectural cause identified; prevention design outlined, not implemented. Existing deployed recovery support remains healthy, pending manual plan remains unapplied.
- Last completed action: reconciled live historical ledgers/run errors, mail and current code; independent review confirmed the prevention gap and acceptance criteria.
- Precise stopping point: before implementation of per-record conflict isolation/trust and before applying the manual recovery plan; no production changes in this investigation phase.
- Next action: prepare a focused implementation/PR for conflict isolation and trustworthy propagation of unaffected prices, preserving the existing immutable history and live-source completeness protections. Do not claim durable resolution until the acceptance gates and scheduled-run observation pass.

## 2026-10-16 — Durable conflict-isolation implementation authorized

- User replied “начинай” to the proposed correction that individual identity conflicts must no longer stop the entire Pharmonline catalog. Implement and verify that persistent behavior; preserve history and refuse incomplete/global/cross-tenant/mass-drift cases. This does not authorize applying the pending old recovery plan or deploying unreviewed new behavior.
- Resume exact saved worktree/branch on Contabo. Active repository main `bf35e70c9e0319d567ae107f30323e05ed031a88` fetched through the explicit current owner URL and merged into the task branch, preserving all append-only journal checkpoints. Shared stale origin/SSH alias remain untouched. Production route remains user-confirmed `13.140.186.143:/opt/pharmacy-monitor`, normal manual GitHub Actions deployment.
- Root is implementing ingestion/schema/trust integration; independent agent maps downstream trust consumers before a shared contract is finalized. Local resources checked: ~15.5 GiB RAM available, 225 GiB disk available, memory pressure zero. Only small sequential targeted checks on Contabo; full PostgreSQL/CI/frontend build gates belong to GitHub CI.
