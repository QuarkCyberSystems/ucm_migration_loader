# Contributing to UCM Migration Loader

## Branches

- `develop`: where work happens. Open pull requests against it.
- `version-16`: the release branch for Frappe v16, the one sites install. It takes changes only from `version-16-hotfix`, at a release.
- `version-16-hotfix`: what the next release takes. A change lands on `develop` first and is backported by its own pull request.

`__version__` in `ucm_migration_loader/__init__.py` is the released version on `version-16` and its hotfix branch, and a development marker on `develop`, `16.0.0.dev0`, so that an image built from `develop` does not pass for a release. A backport keeps the hotfix branch's version: resolve that line, if it conflicts, in favour of `version-16-hotfix`.

## Before you commit

1. Install the hooks once: `pre-commit install`. They run ruff (imports, lint, format), prettier and eslint, as in Frappe's own apps.
2. Write the commit message as a [Conventional Commit](https://www.conventionalcommits.org): `type: summary`, with `type` one of `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `style`, `build`, `ci`, `chore`, `revert`. The summary says what changes, in the imperative or as a noun phrase, without a full stop. CI checks this (`commitlint.config.js`).
3. Run the tests that cover your change: `pytest tests` for what needs no site, and `bench --site $SITE run-tests --app ucm_migration_loader --module <module>` for the rest.

## Rules for the code

- **Data:** use synthetic data only, in tests, fixtures, demo loaders, screenshots and issues. Never real personal data.
- **Permissions:** never bypass the access model (`ignore_permissions`, raw SQL on protected DocTypes) without a reason in a comment next to it, and a test.
- **SQL:** values are always parameters. Interpolate only identifiers and fragments built from code; mark each reviewed case with `# nosemgrep: <rule> -- reviewed: <why>`, as Frappe's apps do.
- **Schema changes** are made in the DocTypes, in the Desk in developer mode. No migration patches: sites are built fresh.
- **Comments** say why the code is written that way. Dates, ticket numbers and test runs belong in the commit message.
- **Frappe:** the app runs on Frappe as released. Nothing in it needs a change to the framework.

## Releases

A release is tagged `v16.0.<patch>`: the Frappe major, the release line (0 until a second one is needed), and a patch that rises at each release, for fixes and features alike. The first is `v16.0.0`.

1. On `version-16-hotfix`, set `__version__` in `ucm_migration_loader/__init__.py` to the next version, such as `16.0.3` after `v16.0.2`. The "Prepare release" workflow does it: run it on `version-16-hotfix` from the Actions tab, and it commits the version on a branch `release/16.0.3` and links the pull request to open into `version-16-hotfix`.
2. Open the pull request from `version-16-hotfix` into `version-16`. Its "Release version" check fails, naming the expected version, when `__version__` is not the next one.
3. Once it is merged, `.github/workflows/release.yml` tags the merge commit `v16.0.3` and publishes a GitHub release, with notes listing the pull requests since the last tag.

Nothing is pushed to `version-16` but the reviewed pull request, so the workflow needs no token beyond its own. The version check is `.github/scripts/release-version.sh`, which you can run before opening the pull request: `.github/scripts/release-version.sh version-16 ucm_migration_loader/__init__.py`.

Install a given release with `bench get-app $URL --branch v16.0.3`; `--branch version-16` follows the latest.

## Pull requests

Describe what changes and why, how it was tested, and anything a reviewer should look at closely (permissions, data model changes, performance). Keep one concern per pull request.

## Security

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).
