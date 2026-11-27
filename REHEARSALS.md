# UCM delegation migration loader

The Python publisher freezes a delegation from a read-only MariaDB snapshot. The Frappe app accepts its sealed file over HTTPS, applies ordered business batches through UCM and reconciles the stored records before opening the context. The full publication includes 21 tables, original identifiers and audit fields, private attachment bytes, and the sensitive-field policy.

## Operating a full delegation wave

1. Provision a target context with its published UUID, status `Locked` and discoverability `Hidden`. Provision the shared dictionary and catalog dependencies and the intended user grants. The full loader preserves the context description. Start a worker on the `ucm_migration` queue.
2. On the publishing host, run `ucm-push source-inventory`, then `ucm-push prepare` as described under [Freeze a full casework publication](#freeze-a-full-casework-publication). Retain the returned run ID and keep the private SQLite file.
3. Run `ucm-push check-publication --site "$TARGET_SITE" --publication "$PUBLICATION" --run "$RUN_ID"`. Give the receiving operator the independently retained run ID, site, complete file SHA-256 and byte count. Save the command’s JSON output as the handoff file. On the receiving bench, bind the reviewed file to its publisher with `bench --site "$TARGET_SITE" execute ucm_migration_loader.operator.approve_sealed --kwargs '{"handoff_path":"/private/path/handoff.json","publishing_user":"publisher@example.invalid"}'`.
4. With the assigned publisher's token in `UCM_MIGRATION_TOKEN`, run `ucm-push send-publication --site "$TARGET_SITE" --url "$UCM_URL" --publication "$PUBLICATION" --run "$RUN_ID" --progress`. Repeating the same command resumes at the target's byte checkpoint. The operator runs `bench --site "$TARGET_SITE" execute ucm_migration_loader.publication_transport.receive --kwargs '{"run":"reviewed-run-id"}'` with the retained run ID to verify and stage the file.
5. In the Migration Run form select **Apply hidden batches**. Use **Pause after current batch** and **Resume from last receipt** when needed. Review the reconciliation and access findings at **Publication ready**, then select **Activate delegation**. The report records the releasing operator and time. **Discard run** removes a hidden wave in reverse order and retains its receipts. A replacement uses a new snapshot identity.

After activation, use the ordinary UCM editing workflow for approved corrections. In the local rehearsal a caseworker corrected a case name after a follow-up remark had been saved. UCM retained the remark, record ID, editor and migration receipt. The loader refused to discard the activated delegation. The reproduction is in `tests/generator_live_correction.py`.

## Four-table development pilot

The original bounded pilot handles Person, Person Other Name, Case and Case Member. The following setup and `plan` / `push` commands reproduce that development profile. The full casework workflow uses `prepare` / `send-publication`.

## Target setup

Install the app alongside UCM. Register a worker named `ucm_migration` under the bench's `workers` configuration, with a timeout of 300 seconds, then start a worker for that queue. The pilot uses **UCM Migration Run** and **UCM Migration Batch**. Full publications also use **UCM Migration Held Share**. System Managers prepare runs, inspect the audit records and control verification, discard and activation. An enabled API publishing account can stage data and check status for each exact manifest assigned to it. It needs no System Manager role. For a dedicated synthetic target context use status `Locked`, discoverability `Hidden` and description `Synthetic UCM migration rehearsal`. Preflight requires the pilot’s business tables to be empty for that context. Each wave uses its own provisioned context UUID on the publishing and target sides.

## Push a publication

On the publishing host, install the Python package and supply a private JSON file with PyMySQL connection arguments for a read-only account on the frozen publication. Plan the run once and give the target operator the manifest file. The command prints the run ID, batch count and document count.

```sh
ucm-push plan --site ucm.localhost --snapshot rehearsed-01 --context "$CONTEXT_ID" --source-config "$PUBLISH_DB_CONFIG" --manifest-out manifest.json
```

On the target bench, review the site, context, snapshot and ordered batch digests. Register the approved file and the enabled publishing account. Replace the example path and account with the reviewed values.

```sh
bench --site ucm.localhost execute ucm_migration_loader.operator.prepare_publication --kwargs '{"manifest_path":"/absolute/path/to/manifest.json","publishing_user":"publisher@example.invalid"}'
```

On the publishing host, set `UCM_MIGRATION_TOKEN` to that account’s API key and secret in `key:secret` format. Set `UCM_URL` to the HTTPS base URL for the target site. The CLI verifies its certificate. For a private certificate authority, set `REQUESTS_CA_BUNDLE` to its CA bundle. To pause after one new batch, then resume in a separate process, run:

```sh
ucm-push push --site ucm.localhost --snapshot rehearsed-01 --context "$CONTEXT_ID" --source-config "$PUBLISH_DB_CONFIG" --url "$UCM_URL" --max-batches 1
ucm-push push --site ucm.localhost --snapshot rehearsed-01 --context "$CONTEXT_ID" --source-config "$PUBLISH_DB_CONFIG" --url "$UCM_URL"
ucm-push status --site ucm.localhost --run "$RUN_ID" --url "$UCM_URL"
```

In a separate operator session using System Manager credentials, request verification and poll until `Verified` or `Rejected`. For separate business commits, run `apply`. Its worker writes a single batch and its receipt in the same transaction, then queues the next batch. The incoming context stays locked and hidden. After the run reaches `Ready`, inspect the report and request activation following sign-off.

```sh
ucm-push verify --site ucm.localhost --run "$RUN_ID" --url "$UCM_URL"
ucm-push status --site ucm.localhost --run "$RUN_ID" --url "$UCM_URL"
ucm-push apply --site ucm.localhost --run "$RUN_ID" --url "$UCM_URL"
ucm-push report --site ucm.localhost --run "$RUN_ID" --url "$UCM_URL"
ucm-push activate --site ucm.localhost --run "$RUN_ID" --url "$UCM_URL"
```

- `received` counts staged batches. `applied` counts batches with committed business rows. `Activated` confirms the delegation is open.
- Use `apply --max-batches 1` to apply one batch. Use `pause` during application. It waits for the current batch transaction and prevents queued work from advancing. Run `apply` again to resume from the durable cursor. A failed validation pauses the worker and leaves a finding.
- Repeat `push` with the same frozen snapshot and limits to resume uploads. New CLI manifests also bind the four MariaDB table definitions. A change in columns, keys or other structural DDL requires review.
- `discard` cancels a pre-activation run. When business batches exist, `Cleaning` means the worker is deleting the run’s rows in reverse dependency order, one transaction per batch. Reissue `discard` after an interrupted cleanup and poll until `Discarded`. Each cleanup checks the saved row digest before deletion. After discard, increment `--attempt` on both plan and push to reload the same published IDs.
- `report` provides per-entity counts and hashes of the matched IDs and populated publication fields. The check includes membership links, alias ownership and child order. Unspecified source fields take UCM defaults. Activation repeats the checks, records the operator and time, and clears staged payloads. Older runs that predate reporting can have empty report fields.

The development profile caps a wave at 500 parent documents and 4 MiB, with at most 256 KiB per upload. Verification rehearses the whole bounded wave through the ORM and rolls it back. The original small-wave route, activating directly from `Verified`, still inserts and opens the wave atomically. Use the explicit `apply` route for batch receipts, reconciliation and recovery testing.

## Reproduce the development rehearsal

From the bench root, with the app installed and the dedicated queue configured:

```sh
env/bin/pytest -q apps/ucm_migration_loader/tests/test_contract.py
env/bin/python apps/ucm_migration_loader/tests/rehearse.py --site ucm.localhost
```

The second command writes synthetic test data. It creates new contexts, clones the deployed columns into connection-local MariaDB publishing tables, calls the application handlers through the bench session, and drains the dedicated queue with burst workers. The fixture omits FULLTEXT indexes because MariaDB temporary InnoDB tables do not support them. A successful run retains the activated synthetic records and audit receipts, disables the synthetic caseworker, and prints the run and context IDs. Rejected and activated run payloads are cleared on discard and activation respectively.

The rehearsal exercises a lost upload acknowledgment, resumption, rollback of earlier ORM inserts after a later invalid link, and an A-only caseworker saving while B is staged.

For the transport rehearsal, supply a separate, socket-only MariaDB server with a persistent data directory. The local OS user needs permission to create a synthetic schema and a temporary read-only login on that server. On this bench the publisher socket is `.ucm-publisher/publisher.sock`. Run from the bench root:

```sh
env/bin/python apps/ucm_migration_loader/tests/transport.py --site ucm.localhost --publisher-socket .ucm-publisher/publisher.sock
```

The rehearsal copies the deployed DDL for all four pilot tables, registers the generated manifest using the bench command, then launches independent CLI processes against the real Frappe app on a loopback HTTPS listener. It checks replay, restart, verification, activation and final relationships. Anonymous requests and an unassigned publisher cannot read the run. Only the assigned publisher or an operator can upload. The test retains the synthetic source schema, activated context and audit receipts, revokes its source login and disables its temporary API accounts.

## Rehearse the first and second delegation on the same site

Provision a fresh rehearsal site on this bench with UCM and the loader installed. Use a new site name for each run. The test checks for existing contexts, migration runs and delegation business data before creating the fixtures. The example uses the standard bench installer, which prompts for the database administrator and site Administrator credentials as needed.

```sh
bench new-site "$FRESH_SITE" --install-app ucm --install-app ucm_migration_loader
env/bin/python apps/ucm_migration_loader/tests/first_wave.py --site "$FRESH_SITE" --publisher-socket .ucm-publisher/publisher.sock
```

For delegation A, the rehearsal cancels an upload after its first receipt and checks that the staged payload was cleared. On the second attempt it deliberately withholds a membership role. The worker checks the staged documents with UCM’s dictionary validator before inserting any business rows. The rehearsal checks the rejection, empty pilot tables and caseworker grant. It then provisions the missing value and reloads the same frozen publication under a new attempt number. It reconciles all four pilot tables and checks the imported relationships and business identifiers.

A synthetic caseworker signs in over HTTPS, finds and edits the imported case, records an activity against it, and creates a new case and person. The same session stays open during B’s migration. It saves an A edit while B is staged, then checks that the edit persisted and B’s case stays out of the caseworker’s list after activation. Both transfers use the persistent publisher, restart the CLI after one batch and retain the run receipts. The test disables its temporary accounts and leaves the synthetic data available for review.

This run covers the four-table pilot with synthetic legacy identifiers, one caseworker and a loopback TLS listener. Qualification still needs broader coverage of generator output, the deployed network gateway and production-sized waves.


### Interrupt a business batch

Run this on a synthetic UCM site that supports a local caseworker password login. It creates new A and B contexts and retains the resulting audit records.

```sh
env/bin/python apps/ucm_migration_loader/tests/batch_recovery.py --site ucm-first-wave-02.localhost
```

The rehearsal terminates a child process between a business insert and its receipt. It checks database rollback, fresh-worker resume, pause, duplicate delivery, out-of-band share and context controls, reverse cleanup and identifier reuse. It also changes a synthetic field after reconciliation and checks that activation waits for correction. A caseworker granted both contexts must still wait for activation to open B’s records.

### Measure live caseworker traffic

```sh
env/bin/python apps/ucm_migration_loader/tests/live_load.py --site ucm-batch-first.localhost --publisher-socket .ucm-publisher/publisher.sock --case-files 150 --batch-size 25
```

This synthetic test reads a frozen MariaDB publication and stages it through the target handlers. A separate authenticated HTTPS session reads and saves an A case during a baseline period and throughout B’s verification, apply and activation. The output gives counts, errors and p50/p95/p99 in milliseconds per phase and operation. Each case file includes a person, alias, case and membership. The caseworker and publishing accounts are disabled or revoked after the run.

To export the current site’s UCM and dictionary inventory for mapping review, run the following with a new output path. Each row identifies its SQL type, context or parent ownership, link target, fetch rule and implemented coverage. Historical policy and unsupported fields remain visible to the reviewers.

```sh
bench --site ucm.localhost execute ucm_migration_loader.inventory.export_csv --kwargs '{"output_path":"/absolute/path/to/field-inventory.csv"}'
```

## Compare an independent expected result

Before activation, give the target operator an independently reviewed projection of the synthetic or legacy snapshot. The expected JSON has `site`, `context`, `snapshot` and an `entities` object with arrays for all four pilot tables. Each parent row has its published `name`, `context_tech_id` and the populated profile fields. A Person Other Name row has its published `name`, `parent`, integer `idx` and populated alias fields. The publisher can perform the comparison over its assigned run's report endpoint.

```sh
ucm-push reconcile --site ucm.localhost --url "$UCM_URL" --run "$RUN_ID" --expected "$EXPECTED_JSON"
```

A mismatch exits with status 1 and lists the entity and the differing checks. A different membership can fail the field comparison even when every table count and key set matches. The command reads expected business values locally and retrieves aggregate hashes from the target. The target operator retains the activation decision.

## Run the complete local qualification suite

From the bench root, choose a new local site name, a new evidence file and the existing populated development target.

```sh
env/bin/python apps/ucm_migration_loader/tests/qualify.py --provision --fresh-site "$NEW_LOCAL_SITE" --target ucm.localhost --publisher-socket .ucm-publisher/publisher.sock --evidence "$NEW_EVIDENCE_FILE"
```

The runner provisions a fresh Frappe site with UCM and the loader, checks the frozen fixture generator output twice, counts target key collisions, rehearses its cases through the ORM, and runs the pilot contract, first and later waves, crash recovery, live traffic and populated-target transport tests. It records the app revisions, local change indicators, installed loader digest, checks, timings and run IDs. Omit `--provision` to use an already installed, empty rehearsal site. The suite retains its synthetic contexts and publications for inspection.

The optional `--access-source-site ucm-generator-access.localhost` adds the published local-script names and effective consent checks to the same qualification run.

## Use the fixture generator as the development source

For generator-based rehearsals, set `UCM_FIXTURE_APP` to the Python package of your installed synthetic fixture app. These optional tests expect its `demo.volume.load` and `demo.volume.check` interfaces and its address-type and member-role helpers. Use the generator on a fresh, isolated publishing site for full-shape test data. The source used here is `ucm-generator.localhost`, hosted on the socket-only publishing MariaDB instance with its web interface in maintenance mode. Its retained snapshot label is `migration-source-96`. The generator ran with 96 persons, two contexts, 12 users, two analysts, two shares per shared-only user, one writer and existing indexes retained. Its own `volume.check` passed. The smaller fixtures above are for targeted faults and known-answer transport checks.

```sh
env/bin/python apps/ucm_migration_loader/tests/generator_coverage.py --source-site ucm-generator.localhost --label migration-source-96 --target ucm.localhost --evidence "$NEW_EVIDENCE_FILE"
ucm-push assess --site ucm.localhost --snapshot migration-source-96 --context "$CONTEXT_ID" --source-config "$PUBLISH_DB_CONFIG"
ucm-push upload-order --site ucm.localhost --snapshot migration-source-96 --context "$CONTEXT_ID" --source-config "$PUBLISH_DB_CONFIG" --batch-size 25
```

`assess` reads the source and returns counts for records and non-default business fields outside the pilot. It inspects tables visible to the publishing login and reports each wave independently. Grant the reader access to the entire publication. The CLI refuses to issue an approval manifest or push a wave that contains these omissions. The checked generator output includes activities, history, sharing, sensitive information and addresses still outside the four-table profile. Its generator sizing presets are inputs for later load tests.

The read-only `upload-order` command stages the service and response dependencies in temporary local SQLite storage and orders the published activity keys into batches. A prerequisite must appear in an earlier batch. Missing, out-of-delegation or cyclic prerequisites fail the plan. The command outputs counts and a reproducible order digest. The generated BOL and SAH activity graphs produced 5 and 9 batches respectively with a limit of 25 keys. The target activity writer is still pending.

The generator evidence includes a comparison baseline for each delegation across the 18 business and access tables declared by the generator, private files attached to people, and tasks assigned on cases and people. It records a digest of every selected stored row, a separate digest of creation/modification times and actors, and a digest of the actual attachment bytes associated with each File ID. Frappe child ownership, position and linked IDs count as stored fields. The baseline covers owned delegation records. Shared reference catalogs, user provisioning and system configuration have separate qualification needs.

Use the following commands from the bench root. The first command reopens the frozen source to verify repeatability. After a complete migration the same command can compare an isolated target containing the original context IDs. The second checks a proposed target for already-used keys and returns counts per delegation. The third rehearses the generated case scalars and ordered child documents through UCM validation, checks a deliberately changed link, and rolls back the transaction.

```sh
env/bin/python apps/ucm_migration_loader/tests/generator_snapshot.py --site ucm-generator.localhost --expected "$NEW_EVIDENCE_FILE"
env/bin/python apps/ucm_migration_loader/tests/generator_collisions.py --source-site ucm-generator.localhost --target ucm.localhost
env/bin/python apps/ucm_migration_loader/tests/generator_cases.py --source-site ucm-generator.localhost --target "$ISOLATED_REHEARSAL_SITE"
```

In the 96-person publication the 20 selected tables contain 2,100 delegation-owned rows, including 96 person files and 70 tasks. Outgoing shares belong to the source delegation. The coverage check lists incoming shares separately because activation may affect the receiving delegation. All 96 person IDs and all 37 case IDs already exist on the populated development target. Use the isolated site for positive generator tests and the populated site for collision checks and uniquely keyed transport fixtures. The generated case rehearsal checks names, numbers, status, remarks and context links. Sensitivity tags still require their own import and reconciliation.

The second publication uses the same generator with 240 people, three contexts and a consent rate of 35 percent. It populated all 18 generator-declared business and access tables. The isolated test opens generated person names, checks the searchable combined names and ordered aliases, inserts each consent through UCM and checks the withholding rules used by the application. The target transaction rolls back after verification. The generator created 33 alternate names and 90 partial objections across the three contexts.

```sh
env/bin/python apps/ucm_migration_loader/tests/publish_generator.py --site "$NEW_GENERATOR_SITE" --preset access --publisher-socket .ucm-publisher/publisher.sock
env/bin/python apps/ucm_migration_loader/tests/generator_consent.py --source-site "$NEW_GENERATOR_SITE" --target "$ISOLATED_REHEARSAL_SITE"
```

Choose an unused `ucm-generator-*.localhost` name. The `casework` preset recreates the smaller sample with newly allocated context IDs. Each source retains its frozen database and private files.

In Desk, open a **UCM Migration Run** and use the **Migration** menu to verify, apply, pause, resume, review the report, activate or clean up as its state permits.

The opt-in `--profile synthetic-case-file-consent-v2` adds Person Consent to the case-file contract. Use the same profile for assessment, planning and upload. Every consent must refer to a person earlier in the wave and supply its recipient, decision and all five withholding flags. Verification uses UCM validation. The worker applies and reconciles the batches before activation. Full generator publications still contain additional unsupported data.

### Shared reference rehearsal

`tests/generator_target.py --site ucm-full-wave-01.localhost` provisions a new isolated target with UCM’s bundled dictionary and the generator’s supplemental terms. Use an unused site name for a new installation. Inspect the dictionary loader’s private report for rejected and suspect source rows.

Run `tests/generator_countries.py --source-site ucm-generator-access.localhost --target ucm-full-wave-01.localhost` with the bench Python to apply and reconcile the global country catalog. Replaying all 250 generated countries reused every key. A conflicting name stopped the rehearsal without changing the existing record.

Run `tests/generator_references.py --source-site ucm-generator-access.localhost --target ucm-full-wave-01.localhost --evidence NEW_FILE.json` to inspect populated external Link fields for each delegation. The report counts distinct references per field, missing target keys and versioned dictionary matches. Sealed publications also bind the first address of each referenced activity place so the receiver can verify the geography UCM derives. Supplemental member roles and address types each had three unique semantic matches despite different UUIDs. The report identifies candidates for the transfer crosswalk. Global catalogs other than countries and dictionaries still require provisioning or mapping.

On the isolated full-wave site, run `tests/generator_geography.py` and then `tests/generator_catalogs.py` using the bench Python. Each accepts `--source-site`, `--target` and a new JSON `--evidence` path. The geography reader selects the administrative areas referenced directly by delegation records and follows their ancestors back to country. The catalog test creates all three target contexts locked and hidden, using the rehearsal marker in their descriptions. It reuses seeded departments and field clearances after comparing their stored values, records their source-to-target keys, and rewrites links to those departments in the service and activity-type catalogs. A NULL and an empty string both represent an unset UCM Select field during this comparison. Retain the crosswalk with the frozen publication for downstream business records.

### Places and activities

On the prepared isolated full-wave target, run `tests/generator_places.py` followed by `tests/generator_activities.py` with the bench Python. Both take `--source-site`, `--target` and a new `--evidence` file. The first imports the places referenced by casework, their addresses and protected place profiles. A singleton address published at position zero is stored at position one by Frappe. The evidence counts this conversion. The loader checks the target clearance covers the same fields before inserting a protected place profile.

The activity rehearsal writes through UCM, records validation and reconciliation findings, then rolls back. Review both the rejected records and the field differences in the evidence. Completing this diagnostic can produce findings that prevent activation. Service closures must follow all the activities within the service. The local publisher assessment independently counts published activities missing a mandatory place, including those that cannot reach validation because a prerequisite failed.

### Linked casework and private pictures

Run `tests/generator_persons.py` or `tests/generator_casework.py` with the bench Python and the same source, isolated target and evidence options as the activity rehearsal. The person reader checks dictionary identities and applies the crosswalk to both the parent and children. It verifies each private picture against the source File metadata, inserts it through Frappe and compares the actual target bytes and attachment ownership. The source site's private files must be available beside the MariaDB publication for these local tests.

The linked casework rehearsal preflights mandatory places per context. Within each eligible context it inserts people, cases, activities, case links, relationships, consent, history, notes, sensitive records, tags and assigned tasks. It checks the published historical lock actor and timestamp, exercises UCM's lock permission, and reconciles history after the bulk latest-state refresh. The rehearsal provisions missing synthetic assignees as disabled accounts within the same rollback-only transaction. It does not grant real users access. Activity geography filled by UCM is compared with the frozen place address, and additional controller-derived fields are reported by field name. This rollback-only rehearsal covers casework and historical audit fields. The sealed-wave tests below cover committed recovery, access release and cancellation.

### Check the delegation inventory

Run `ucm-push source-inventory --site "$TARGET_SITE" --context "$CONTEXT_ID" --snapshot "$SNAPSHOT" --source-config "$PUBLISH_DB_CONFIG"` with read access to the entire publication. The result gives counts by stored context and identifies business records outside the supported casework profile. Context Case carries the receiving context, so its context-column count can differ from the number of rows published under the owning case. The full publisher includes Organization and organization-owned Sensitive Information. It reports unsupported owner types and inconsistent sensitive-record ownership before transfer. Previous 20-table casework rehearsals cover a subset of delegation data.

### Freeze a full casework publication

The standalone client can materialize the 21 delegation tables, their stored audit columns, ordered child rows, reference identity attributes and private attachment bytes into a local SQLite file. Run the following on the publishing host with a new output path in private storage.

```sh
ucm-push prepare --site "$TARGET_SITE" --context "$CONTEXT_ID" --snapshot "$SNAPSHOT" --source-config "$PUBLISH_DB_CONFIG" --private-files "$SOURCE_PRIVATE_FILES" --batch-size 25 --publication-out "$PUBLICATION"
ucm-push check-publication --site "$TARGET_SITE" --publication "$PUBLICATION" --run "$RETAINED_RUN_ID"
```

For row-level correction, add `--findings-out "$FINDINGS_JSONL"` to the freeze command and choose an unused path. If activities lack a mandatory place, freeze exits with an error before creating a publication. Each private JSON Lines entry identifies its snapshot, context, activity ID, field, activity type and required place type. The source owner uses the original casework evidence to correct the publishing input and issues a fresh snapshot. Rerun freeze with fresh output paths. An eligible snapshot produces no findings file.

Freeze records the available Frappe and UCM versions and a SHA-256 fingerprint of the referenced dictionary term identities. Supply `--ucm-version` if the publishing database has no installed-application metadata. Staging, application and activation check the declared versions against the target.

For new publications the manifest includes the field-to-clearance assignments, clearance kinds and context exemptions. The worker compares these with the receiving site before the first business write, at reconciliation and again before go-live. Organizations precede their protected rows.

Keep the run ID independently of the file. It binds the context, site, snapshot, ordered batch hashes and reference identities. Each attachment's SHA-256 digest is included in its person's batch. Equal photo contents are stored once, and each attachment retains its own File row. The SQLite file contains sensitive values and private bytes. The publisher creates it with owner-only permissions. Publishing freezes and verifies locally and does not require network credentials for the target.

Full publications use the original context description. Select an empty, locked and hidden receiving delegation. The operator staging the sealed publication binds it to that context, and a staged run prevents an early release.

On the receiving bench, `ucm_migration_loader.publication_receiver.stage(path, run)` is a System Manager operator entry point for a delivered file. It copies to the target site's private migration directory, verifies the retained run ID, locks the receiving context and records a durable receipt. A second submission returns the existing receipt. `Publication staged` means the target has verified the complete file. Its applied cursor stays at zero until a business worker commits records. The benchmark exercised this local handoff on the isolated target. The sealed HTTPS handoff and both operator workflows are described below.

### Choose write routes on the receiver

Open a staged full-publication **UCM Migration Run** in Desk and choose **Migration → Configure write routes**. The form lists parent DocTypes present in that publication. Each defaults to **Document API**. After the first apply request the plan locks. Desk and CLI apply and resume use the same plan. Open a new batch receipt to see its published DocType and actual write route. Older receipts predate these fields.

Every published parent DocType can use validated SQL. Child records and private attachments use the parent's writer. The worker validates each document, preserves publication order, and runs post-insert hooks before processing dependent records. Person and note batches group multiple parents in one SQL insert. A Person batch flushes preceding rows before validating a link to a person in that same batch. Deferred assignments and shares use the saved route when eligible for release. Add `--mixed-routes` to the rehearsal below to exercise both writers.

### Sealed generator waves

The complete generator rehearsal starts with a new local site, installs UCM and the dictionary, provisions shared catalogs, freezes and independently checks each delegation, stages it, applies it and releases it. Specify an unused site and evidence path.

```sh
env/bin/python apps/ucm_migration_loader/tests/generator_end_to_end.py \
  --source-site ucm-generator-access.localhost \
  --fresh-site ucm-full-wave-example.localhost \
  --caseworkers 4 --test-held-shares \
  --evidence test-results/full-wave-example.json
```

For the 2,400-person generator installation use `--source-site ucm-generator-scale.localhost --snapshot-label migration-scale-2400` and another fresh target. The evidence records requests, errors and p95 latency per phase, the final edit for each of four caseworkers, and receipt counts for historically accepted and pending shares. The test also verifies that a separate inactive delegation's shares stay held.

| Recovery rehearsal | How to reproduce from the bench |
|---|---|
| First delegation | Run `tests/generator_first_cancellation.py` with `--source-site`, `--fresh-site` and a new `--evidence` path. It cancels after two committed batches, restarts cleanup, verifies the empty baseline and activates a fresh snapshot. |
| Later delegation | Provision another empty site with `tests/generator_target.py --site SITE`. Run `tests/generator_cleanup.py --site SITE --source-site SOURCE --work PRIVATE_DIR --evidence NEW_JSON`. It opens BOL, commits CHA's hidden batches, cancels CHA and compares BOL's before and after snapshots. |
| Replacement and live correction | Run `tests/generator_reload_wave.py` on the cleanup site with `--site`, `--source-site`, `--previous-run` from the cleanup evidence and a new `--evidence` path. Then run `tests/generator_live_correction.py` with `--site`, the replacement `--run` and another new `--evidence` path. |

Use `env/bin/python` and script paths relative to `apps/ucm_migration_loader/` for these rehearsals.

A receiving operator runs `ucm_migration_loader.publication_worker.apply(run, max_batches)` against the private staged copy. The default is one transaction. Run it again from another process to resume. The applied cursor counts committed batches. The worker reports the first deferred access batch when casework is complete. The operator runs `ucm_migration_loader.publication_worker.activate(run)` to check the stored casework and files, refresh and verify sensitivity, restore assignments and shares, and release the context in one transaction. The sealed manifest binds each run to its target site. Application also checks the locked, hidden migration context. Generated historical assignment recipients are provisioned as disabled users by the test fixture.

For HTTPS delivery the operator registers the independently obtained run ID, publisher account, complete file SHA-256 and byte length with `ucm_migration_loader.publication_transport.approve(run, publisher, sha256, size)`. The Python client uses `ucm-push send-publication --site SITE --url HTTPS_URL --run RUN --publication FILE` with the publisher token in `UCM_MIGRATION_TOKEN`. It resumes from a durable byte offset. The receiver runs `ucm_migration_loader.publication_transport.receive(run)` to reassemble and check the approved file, then stage it. Transfer, application and activation have separate receipts. Private inbox chunks are retained on the rehearsal site.

### Operating and cancelling a sealed publication

On the Migration Run form, select **Apply hidden batches**, **Pause after current batch**, **Resume from last receipt**, **Activate delegation**, or **Discard run** as appropriate. The same commands are available through the existing operator API. Use a worker on the `ucm_migration` queue. The full-publication worker schedules up to 25 batches per queue job and starts no batch after the job's first 120 seconds, so that batches of a thousand records with pictures, which take close to a minute each, stay inside the queue's timeout. Each business batch commits separately. Bounded requests retain an absolute stopping cursor on replay, and a newer command supersedes queued work at the next transaction boundary. The run becomes **Publication ready** after checking the source manifest, committed receipts, UCM option sets, context links, stored rows and deferred access inventory. The reviewer can download that reconciliation from Desk. Activation runs those checks again.

New publications include the MariaDB definitions for 21 owned tables. Previously staged 20-table runs retain their original receipts and scope. A new staging request requires a publication that includes organizations and the sensitive field policy. The signature ignores the order in which indexes were declared and the mutable AUTO_INCREMENT counter. It preserves the columns within each index. `publication_receiver.preflight(path, run)` reports schema differences, context existence and counts of target key collisions before staging. Before its first business write, the worker inventories all incoming identities and resolves references, including audit actors, against the target or the same publication.

Discard processes committed batches in reverse order. Each step locks the context and run, verifies its source receipt, validates record ownership and attachment inventory, and deletes through Frappe. It commits the reduced cursor with the deletes. Restart cleanup from Desk or use `ucm_migration_loader.publication_cleanup.discard(run, max_batches)` on the receiving bench. Original receipts remain available after discard. A new snapshot ID identifies a replacement publication. Activated waves require normal live-data change procedures.

**Framework dependency for cancellation.** This workspace adds the keyword-only `delete_dynamic_links_now` option to `frappe.model.delete_doc.delete_doc`. Deploy that Frappe change with this loader when enabling full-publication cleanup. The [framework patch](dist/frappe-16.52.0-migration-cleanup.patch) was qualified against Frappe 16.52.0 at commit `4515014c87`. On an unpatched checkout, run `git apply --check` against the patch from the Frappe repository before applying it. It runs Frappe's ordinary cleanup of linked assignments, comments and related records in the deletion transaction. A delayed job for a discarded ID could otherwise remove assignments belonging to its re-import. The loader passes the keyword explicitly so an older framework fails before deleting a record.


For a sealed delivery, `ucm-push transfer-status --site SITE --url HTTPS_URL --run RUN` reads the target's byte checkpoint. `send-publication --progress` prints acknowledged bytes on stderr and returns its final JSON receipt on stdout. After staging, `status` reports the business batch cursor and `report` returns the reviewer’s reconciliation. The report includes the published counts, checked business rows, deferred and released access rows, and the SHA-256 digest of the ordered batch sequence. A failed queued operation records its batch and item when available, without copying a person's field values into the run finding. Resolve the reported batch and item to the exact publishing ID with `ucm-push locate-item --site "$TARGET_SITE" --publication "$PUBLICATION" --run "$RUN_ID" --sequence 1 --item 1`.


### Historical shares to future delegations

An open historical share to an inactive receiving delegation creates a System Manager-only **UCM Migration Held Share** receipt when its owner goes live. The published share ID, source run, batch and item position bind it to the original publication. The source report distinguishes live access rows from held receipts. Keep the source site's private staged copy until all of its holds are resolved.

Recipient activation checks the sealed descriptor, reference identities and source batch digest. It evaluates current UCM consent, expiry, record ownership, merged-person status, receiver status and existing shares. An eligible item retains the published ID and status. A consent objection or other eligibility change leaves the receipt in **Review** without creating a Record Share. An integrity, permissions or storage error rolls back the activation transaction. Source and recipient activation are serialized so overlapping go-lives cannot miss a hold. Replay of a completed activation returns its receipt. The source run links to historical share receipts and the receiving run links to shares requiring review.

### Comparing ORM and direct SQL at 50,000 people

The `wave50k` generator preset puts exactly 50,000 people in CHA and 250 in each neighbouring context. It uses the existing generator rules, catalogs and attachment pool. The experiment uses a dedicated publishing MariaDB and two isolated receiving databases. The production loader continues to use the document API.

`tests/benchmark_raw_copy.py` streams CHA’s 21 tables into new, indexed InnoDB tables on the SQL target. It commits every 1,000 rows, hashes the source rows and separately rereads and hashes the target. Its time covers database rows and attachment metadata. A successful raw copy has no activation receipt.

`tests/benchmark_publication.py` freezes the business batches once and binds a copy to each receiving site. The batch chain, reference mappings, attachment hashes and row counts must match. The timed interval starts with a staged publication and an active BOL context.

`tests/benchmark_routes.py` runs one route at a time. Four BOL caseworkers read and save while CHA is loaded. Both routes deliberately reject a bad link and apply the same reconciliation, consent, protected-field, attachment and activation checks. The experimental SQL writer prepares UCM documents, executes their validation methods, inserts parents and children with MariaDB multi-row inserts and runs the post-insert hooks. Historical locks, tags, file storage, assignments and share operations retain their dedicated UCM commands. A separate timer measures the multi-row SQL statements. This writer is confined to the benchmark site.

The benchmark scripts use the bench Python and the loader source directory on `PYTHONPATH`. After provisioning the two empty targets named in `benchmark_publication.py`, run its `freeze`, `orm` and `sql` phases against the same private `--work` directory. Run `benchmark_routes.py orm` and `benchmark_routes.py sql` sequentially with that directory. Use `benchmark_compare.py` to check both activation receipts, compare their batch chains and access outcomes, and combine those results with the independently verified raw-copy measurement.

### Checkpoint and live-load recovery

On a disposable site, stage a fresh generated context containing 60 Persons and 60 Person Notes in batches of two. From the bench sites directory, run the following with the new run digest. This rehearsal takes ownership of the fixture, sets its write routes, pauses apply using a slow synthetic request, interrupts reconciliation, resumes at the saved receipt, changes a target value without touching its modification timestamp, verifies activation rejects it, restores the value and activates the delegation.

```sh
../env/bin/python ../apps/ucm_migration_loader/tests/checkpoint_recovery.py --site REHEARSAL_SITE --run RUN_DIGEST
```

### Set-based SQL route

`ucm_migration_loader.ucm_migration_loader.set_based` loads a staged publication without saving each record through its controller. An operator chooses it for a staged run ([operator guide](README.md#set-based-sql-route)); the migration queue then runs it in jobs, each taking one step from where the last one stopped. It uses the same staged file, hash index, run record, batch receipts and hidden context as the document route. Sensitivity tags, assignments and shares still go through the documents.

The same steps can be called one at a time from the receiving bench, which is how the timings below were taken:

```sh
bench --site "$TARGET_SITE" execute ucm_migration_loader.ucm_migration_loader.set_based.apply --kwargs "{\"run\":\"$RUN_ID\"}"
bench --site "$TARGET_SITE" execute ucm_migration_loader.ucm_migration_loader.set_based.derive --kwargs "{\"run\":\"$RUN_ID\"}"
bench --site "$TARGET_SITE" execute ucm_migration_loader.ucm_migration_loader.set_based.verify --kwargs "{\"run\":\"$RUN_ID\"}"
bench --site "$TARGET_SITE" execute ucm_migration_loader.ucm_migration_loader.set_based.activate --kwargs "{\"run\":\"$RUN_ID\"}"
```

- `apply` writes the published rows with multi-row inserts. Each group of batches of one DocType, up to 5,000 parent records, commits with its receipts and the run's cursor. Run it again to resume.
- `derive` applies the values a save would derive, for this delegation only: an activity's place type, country and areas from its Place, the department and country copies on history rows and notes, the share facts and the latest-state marks. It returns the rows each rule changed.
- `verify` compares every stored column with the publication, counts the rows that break the rules a query can check (required fields, options, links, option sets and collections, links that leave the context, places, history locks), and passes one record in fifty through the document route's own check. With no finding the run becomes **Publication ready**; with findings it is paused. The report is kept on the run.
- `activate` releases the held assignments and shares through the documents and opens the context in one transaction. It reads the counts and receipts again and does not re-read every record.
- `discard` removes a hidden run's rows by primary key, 5,000 at a time, and needs no change to Frappe.
- `advance` is what a queued job calls: it finds the step the run is at and takes it. A job is given 120 seconds of batches (`BUDGET`), and a verification saves its cursor every ten seconds (`CHECKPOINT`).

No controller runs for a copied row. A rule of a controller that is not in `verify`'s list is checked only on the sampled records. A picture keeps its published file name; the document route may store the same bytes under another name.

**Measured on a development site.** One synthetic delegation of 10,000 persons, 242,786 business rows and 70 assignments in 223 batches of 1,000, loaded beside 24.8 million rows of other delegations, on a database with a 128 MB buffer pool. Each figure is one run.

| Step | Set-based SQL | Document API |
|---|---|---|
| Stage the sealed file | 10.5 s | same step |
| Write the business batches | 108.3 s | |
| Derive | 11.6 s (4,163 activities took their geography from their Place) | |
| Verify (every column, 242 rules, 4,305 records through the documents) | 38.3 s | |
| Staged to **Publication ready** | 2.8 min | 48.7 min |
| Activation, rolled back | 4.6 s | not timed |
| Discard | 78 to 242 s over five runs | not timed |

Both loads were compared row by row and field by field over every table of the publication. All 242,786 rows are on both sides and equal, except the file name of the 10,000 private pictures (`File.file_url` and `Person.person_picture`), which hold the same bytes. The row counts of all 504 tables of the site changed by the same amounts. The document route's own reconciliation of every record passed over the rows the set-based route had written (23.9 min).

**A larger delegation, and shares.** Two delegations of the 50,000-person generator source were loaded on the same site through the bench's own worker, under keys of their own (see [Re-keyed generator sources](#re-keyed-generator-sources)). Each figure is one run.

| | Small delegation | Large delegation |
|---|---|---|
| Published rows | 37,485 in 52 batches | 1,254,720 in 1,090 batches (1.48 GB sealed) |
| Persons, activities, history rows | 250, 797, 2,096 | 50,000, 170,137, 445,382 |
| Assignments and shares | 20 and 73 | 10 and 11,015 |
| Write | 9 s | 331 s, of which 105 s for 5,701 sensitivity tags through the documents |
| Derive | 5 s | 70 s (18,845 activities took their geography from their Place) |
| Verify | 5 s | 218 s: every column of 1,243,695 rows, 242 rules, 21,400 records through the documents |
| Request to **Publication ready** | 23 s | 10.5 min |
| Activation | 2 s, committed | 167 s, rolled back |
| Discard | not run | 371 s |

The small delegation was activated through the queue while the other two delegations of the source were not live: its 20 assignments and its 13 closed shares were released, and its 60 open shares became held share receipts. The large delegation's activation then ran as one transaction of 167 s: 10 assignments and 4,169 shares released through their documents (3,308 to the delegation already live, 861 closed ones), 6,846 held for the delegation not yet live, and 35 of the small delegation's held shares released to it. It was rolled back, so the large delegation stayed hidden.

The first attempt at the large delegation was stopped by the verification at batch 148: a sampled activity carried a list of tag codes that differed from the publication. The cause was in the re-keying tool, which had renamed a tag code but not its occurrences inside lists; the route itself behaved as intended, paused the run and named the batch and item. That load was discarded (the 371 s above) and a corrected copy loaded.

Not covered: a share that UCM's current consent rules no longer allow (none occurred), a delegation larger than 1.25 million rows, and a load while caseworkers work in other delegations.

`tests/set_based_comparison.py` makes this comparison: a row-by-row and field-by-field diff of every table of the publication, and the row count of every table of the site before and after. `tests/test_set_based.py` covers the parts that need no site.

**Through the queue.** `tests/set_based_queue.py` also has `worker`, `activate` (with `--dry-run` to roll back) and `discard`, which ask as Desk does and follow the bench's own worker. Its `rehearse` stages a sealed file, chooses the route and runs the queue's jobs in its own process with a few seconds of budget a job. On the way it checks a pause for live load, a pause asked by the operator, a failure injected into the verification and the resume at the saved batch, the report, an activation refused after a receipt was changed, an activation timed and rolled back, and the discard over several jobs. The run ends discarded. `tests/set_based_queue.py worker` asks for the load as Desk and `ucm-push` do and follows the bench's own worker. `e2e/set_based_route.py` does the same in a browser: it chooses the route in the dialog, applies, and waits for **Publication ready**.

On the same delegation the rehearsal reached **Publication ready** in 134 s, pauses and the injected failure included, over 23 jobs of five seconds, and discarded it in 242 s over 33 jobs. Chosen and applied in Desk and carried by the bench's own worker, which serves every site of the development bench, the run was ready 4.0 min after its first receipt: 181 s of writing (36 s of them for 1,175 sensitivity tags through the documents), 13 s of deriving and 42 s of verifying.

```sh
env/bin/python apps/ucm_migration_loader/tests/set_based_queue.py rehearse --site REHEARSAL_SITE --run RUN_DIGEST --publication /path/to/publication.sqlite
env/bin/python apps/ucm_migration_loader/e2e/set_based_route.py --site REHEARSAL_SITE --run RUN_DIGEST
```

### Re-keyed generator sources

The generator computes its record keys from an entity code and a sequence number, so two sites it filled hold the same keys, and a delegation of one cannot be published to the other as it is. `tests/rekeyed_source.py` reads a generator source through a view that moves every key a delegation owns to a key space of its own and leaves the shared reference data (countries, areas, places, shared activity types) under the keys both sites know. Nothing is written to the source.

```sh
env/bin/python apps/ucm_migration_loader/tests/rekeyed_source.py plan --plan PLAN.json --source-site SOURCE_SITE --site TARGET_SITE --prefix W50
env/bin/python apps/ucm_migration_loader/tests/rekeyed_source.py prepare --plan PLAN.json --commit
env/bin/python apps/ucm_migration_loader/tests/rekeyed_source.py freeze --plan PLAN.json --context W50BOL --snapshot-id NEW_SNAPSHOT --publication /path/to/new.sqlite
```

- `plan` reads both sites and keeps what moves: the entity codes the delegations own, the few owned rows of catalogs that also hold shared rows, the shared sensitivity tags the two sites have under different keys, and the codes that must be unique on the target (context codes and local tag codes take the prefix).
- `prepare` creates the source's contexts on the target, locked and hidden, each with the catalogs it owns. Without `--commit` it shows what it would create and rolls back.
- `freeze` seals one delegation under its new keys. Check the file with `publication_receiver.preflight` before staging: `existing_keys` must be empty.

`tests/test_rekeyed_source.py` covers the view without a site. The tool is for rehearsals with generator data only; a real publication carries the keys of its source system.
