# Development and tests

Three kinds, each run where it fits. Synthetic data only.

| Kind | Where | Run with | Needs |
|---|---|---|---|
| Unit tests | `tests/test_*.py` | `pytest` from the app folder | Frappe importable; no site, no database, no ucm |
| Integration tests | `ucm_migration_loader/tests/` and beside the DocType | `bench --site SITE run-tests --app ucm_migration_loader` | a site with ucm and the loader installed |
| Rehearsals and browser tests | the other files of `tests/`, and `e2e/` | by hand, from the bench; see [REHEARSALS.md](../REHEARSALS.md) | a development site that is being served, with a worker and generator data |

- The unit tests cover what needs no site: the order of activities and the tables' fingerprints, the sealed file's index, the set-based route's planning, the run's overview and metrics, `ucm-push quality` with the site's answers played back, and the short commands with their settings file.
- The integration tests cover what the site decides: who may approve and act (the Migration Admin role), who may propose (the Migration Publisher role), the proposal with its check code, the approval of a hand-off with its context, the run that waits for its file and its staging on arrival, who may read a run and its metrics, the hand-over record, a sealed file that is refused for each reason and staged again afterwards, the account a transfer names, the list's columns, and what the form shows in every state. They build their own runs and a small sealed file by hand, roll everything back and remove the files they stage, so they can run on a site that holds loaded delegations. They do not load a delegation: a load commits, and is rehearsed instead.
- The rehearsals load, verify, hand over, activate and discard real generator delegations through the bench's own worker, and the browser tests drive the Desk form through the same steps.
- `e2e/manual_screenshots.py` takes the pictures of the user manual from runs in the situations it describes, into `docs/images/user-guide/`; `e2e/site_operator.py --shots` takes those of the approval. The manual has one source, `docs/user-manual.src.html`: `python docs/build_manual.py` builds from it the page the repository shows (`docs/user-manual.md`, with the pictures beside it) and the one-file manual (`docs/user-manual.html`, with the pictures inside it). A unit test fails when either is not what the source builds.
- `tests/set_based_kill.py` ends the job of a set-based load at seven points (inside a write transaction, between two, during the derive, the verification, the conclusion, the activation and the discard), by killing its process or cutting its database connection, and checks what is left and that the resumed load is the same as an undisturbed one.

**When UCM changes a rule.** The set-based route states UCM's save rules again as queries, so a rule UCM adds or changes has to be followed. Two integration tests watch for it (`ucm_migration_loader/tests/test_rules_against_ucm.py`), and the CI runs them every night against UCM's develop branch:

- *The code a save runs.* For each table the route writes, the hooks of UCM's controller and the UCM functions they call are fingerprinted (comments and layout aside) and compared with `ucm_rules_reviewed.json`. A difference fails the test and names what is new, gone or changed. Read the change, add or adjust the rule in `set_based` where the route copies it, then keep the new reading:

  ```sh
  bench --site DEVELOPER_SITE execute ucm_migration_loader.ucm_migration_loader.ucm_rules.review
  ```

- *The same records, read both ways.* Records saved through UCM's documents must break none of the route's rules; a record UCM's document refuses (a value outside its options or its list, a required field left empty, a link to another context's record, a place its activity type does not allow, a relationship recorded twice) must be found by the rule that names it; and the place type and address a save gives an activity must be what the route's derive step gives it.

The fingerprint follows a hook and the functions it calls directly, not what those call in turn.

`.github/workflows/ci.yml` runs the unit tests and, on MariaDB 11.8 and Frappe version-16, the integration tests. The integration tests need ucm, whose repository is private: set the repository secret `UCM_CLONE_URL` to a clone URL that can read it, or that job is skipped with a warning. `.github/workflows/linter.yml` runs the commit-title check, pre-commit (ruff, prettier, eslint), Frappe's Semgrep rules and pip-audit on every pull request, and `.github/workflows/betterleaks.yml` scans its commits for secrets. Run `pre-commit run --all-files` before committing. [CONTRIBUTING.md](../CONTRIBUTING.md) has the branches, the rules for the code and the releases; [SECURITY.md](../SECURITY.md) says how to report a vulnerability.
