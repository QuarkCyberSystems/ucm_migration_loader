# UCM Migration Loader

Moves one delegation at a time from a publishing database into a shared UCM site. The delegation travels as one sealed file. It is loaded into a context that stays locked and hidden, checked there against the file, and opened to its users only when the migration team decides so.

## How a delegation moves

Two people do it, each from where they work; a third prepares the site once. The site connects to nothing on the publishing side: every exchange starts from the publishing host.

| Who | Works in | With |
|---|---|---|
| Publishing operator | A terminal on the publishing host | The `ucm-push` command |
| Migration team | The UCM Desk | The **Migration Admin** role |
| Site administrator | The UCM server, once | The app, its worker and the two roles |

| Step | Who | How |
|---|---|---|
| 1. Seal | Publishing operator | `ucm-push seal DEMO01` writes the delegation into one file, with a fingerprint. A delegation the site could not load is refused here, in a sentence |
| 2. Propose | Publishing operator | `ucm-push propose` sends the site a summary of the file, never the file, and prints a check code |
| 3. Approve | Migration team | In the Desk, open the proposed run, click **Approve hand-off** and type the check code the operator told them |
| 4. Send | Publishing operator | `ucm-push send` transfers the file in pieces over HTTPS; a transfer that is interrupted goes on where it stopped. The site compares the file with the approved fingerprint and stages it by itself |
| 5. Load | Migration team | **Migration → Configure write routes**, then **Apply hidden batches**. The records are written out of sight and compared with the file |
| 6. Review | Migration team | The run's form shows its steps, the rows on both sides and any finding |
| 7. Hand over | Publishing operator | `ucm-push quality` collects the run's metrics for the data quality database |
| 8. Decide | Migration team | **Activate delegation** opens the delegation to its users; **Discard run** removes every record of it. Either way the site deletes its copy of the file |

![A verified run waiting for the go-live decision: its steps with their times, the run's facts and the rows on both sides](docs/images/user-guide/run-ready.png)

Until activation nothing is visible: a delegation that is loading, paused or verified is hidden from every UCM user. Activation is the only step that cannot be taken back.

**The [user manual](docs/user-manual.md) takes each of the three through their part, step by step, with a picture of every screen.**

## Documentation

| Page | For | Holds |
|---|---|---|
| [User manual](docs/user-manual.md) | The publishing operator, the migration team, the site administrator | Every step with its screen; what the site must hold; preparing the site; what to do when something goes wrong. The same manual in one file, to read without the repository: [docs/user-manual.html](docs/user-manual.html) |
| [Limits and edge cases](docs/limits-and-edge-cases.md) | Whoever plans a migration | What the loader refuses, holds back or leaves out, and at which step each case is found |
| [Technical reference](docs/reference.md) | Whoever runs and maintains the loader | Every `ucm-push` command, the run's form and states, the data quality metrics, recovery, the set-based route and its measurements, larger publications |
| [For scripts](docs/detailed-commands.md) | Automation | The detailed commands that take every value as an option, and the bench's commands for a site without a browser |
| [Development and tests](docs/development.md) | Contributors | The three kinds of tests, the rehearsals, and what to do when UCM changes a rule |

## Requirements

- **Publishing host:** Python 3.11 or newer; an account that only reads the publishing database, whose UCM tables are those of the site, column for column; the private files of the published persons' pictures; HTTPS access to the site; the API key of a publishing account on the site.
- **Receiving site:** Frappe version 16 as released, with UCM installed; a worker for the loader's own queue; an HTTPS address; room on the disk for one sealed file per delegation in flight.

## Install

In the commands below, `https://github.com/ORGANISATION/REPOSITORY.git` stands for the address of your organisation's repository of the loader; you need read access to it. Whatever the repository is called, the app, and its folder in a bench, is `ucm_migration_loader`.

**Publishing host.** Use Python 3.11 or newer. The CLI works independently of Bench. The source database must already contain the mapped UCM tables with matching DDL.

```sh
git clone https://github.com/ORGANISATION/REPOSITORY.git ucm_migration_loader
cd ucm_migration_loader
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
ucm-push --help
```

To take a newer version later, from that folder:

```sh
git pull
. .venv/bin/activate
python -m pip install .
```

**Receiving host.** In a Frappe bench with UCM installed on the target site, install the app and sync its DocTypes. The app runs on Frappe as released: it needs no change to the framework.

```sh
export TARGET_SITE=ucm.example.org
bench get-app ucm_migration_loader https://github.com/ORGANISATION/REPOSITORY.git
bench --site "$TARGET_SITE" install-app ucm_migration_loader
bench --site "$TARGET_SITE" migrate
```

To take a newer version later, pull it, migrate, and restart every process of the bench, the workers included: a worker keeps the code it started with.

```sh
git -C apps/ucm_migration_loader pull
bench --site "$TARGET_SITE" migrate
bench restart        # or restart the bench's processes as this environment runs them
```

Register the queue under the existing `workers` object in the bench's `sites/common_site_config.json`. Merge the following entry with any existing queues. Start the worker under the bench process manager used for this environment. For a foreground run:

```json
{"workers": {"ucm_migration": {"timeout": 300, "background_workers": 1}}}
```

```sh
bench worker --queue ucm_migration
```

The two roles, the publishing account and the checks of the address and the disk are in the user manual: [Prepare the site](docs/user-manual.md#prepare-the-site).

## Configuration

**On the publishing host,** `ucm-push init /srv/migration` writes three files to fill in, readable by the operator's account only: the settings and two database connections. The [samples](samples/) folder shows them with every key explained.

```toml
site = "ucm.example.org"                                    # the receiving site, as its bench names it
url = "https://ucm.example.org"                             # its HTTPS address
source_config = "/srv/migration/credentials/source.json"    # read-only database connection (PyMySQL)
private_files = "/srv/publication/private/files"            # the pictures of the published persons
publications = "/srv/migration/publications"                # a folder per snapshot is made here
token_file = "/srv/migration/credentials/ucm-token"         # key:secret of the publishing account, mode 600
quality_config = "/etc/ucm-migration/quality-db.json"       # optional: the data quality database
```

```json
{
	"host": "publisher-db.example.org",
	"port": 3306,
	"user": "migration_reader",
	"password": "<password of the read-only account>",
	"database": "ucm_publication"
}
```

**On the site,** set with `bench --site <site> set-config`; a running site reads a changed setting within a minute. All are optional.

| Setting | Effect |
|---|---|
| `ucm_migration_route` | The write route a newly staged run starts with. Without it, the Document API |
| `ucm_migration_sample_every` | One record in so many goes through UCM's own check during the verification. 50 by default |
| `ucm_migration_live_p95_ms`, `ucm_migration_max_lock_waits` | The limits beyond which a load pauses for the site's users |
| `ucm_migration_quality_url` | A link with `{run}` in it: the run's form then offers **Open in data quality dashboard** |

## Limits to know before a migration

- **The tables must be the same.** The publishing database and the site must have the same columns in every table a publication carries. A delegation's own fields therefore have to be columns of the site, and of every publishing database, before its file is sealed; and the site's tables and UCM release stay as they are from sealing to activation.
- **Shares wait for their receiver.** A share to a delegation that is not live yet is held, and released when that delegation is activated. A record that links to another delegation's record along a share is not carried yet.
- **A publication carries a delegation's own records.** Dictionaries, catalogs, users, teams and security profiles are expected on the site. Files other than a person's picture, comments and versions are not carried.
- **A delegation is loaded once,** into a context that is locked, hidden and empty.
- **The Document API route is for small delegations;** the set-based route is the one for a large delegation.
- **What was rehearsed:** the largest delegation loaded had 1.25 million rows; the largest file sent over HTTPS was 295 MB.

Every case, with the step at which it is found: [Limits and edge cases](docs/limits-and-edge-cases.md).

## Development

Unit tests run with `pytest` from the app folder; integration tests with `bench --site SITE run-tests --app ucm_migration_loader` on a site with UCM and the loader; rehearsals and browser tests by hand on a development site. Synthetic data only. See [Development and tests](docs/development.md), [CONTRIBUTING.md](CONTRIBUTING.md) for the branches and the rules of the code, and [SECURITY.md](SECURITY.md) to report a vulnerability.
