# UCM delegation migration loader

The Python publisher freezes a delegation from a read-only MariaDB snapshot. The Frappe app accepts its sealed file over HTTPS, applies ordered business batches through UCM and reconciles the stored records before opening the context. The full publication includes 21 tables, original identifiers and audit fields, private attachment bytes, and the sensitive-field policy.

## Operating a full delegation wave

1. Provision a target context with its published UUID, status `Locked` and discoverability `Hidden`. Provision the shared dictionary and catalog dependencies and the intended user grants. The full loader preserves the context description. Start a worker on the `ucm_migration` queue.
2. On the publishing host, run `ucm-push source-inventory`, then `ucm-push prepare` as described under [Freeze a full casework publication](#freeze-a-full-casework-publication). Retain the returned run ID and keep the private SQLite file.
3. Run `ucm-push check-publication --site "$TARGET_SITE" --publication "$PUBLICATION" --run "$RUN_ID"`. Give the receiving operator the independently retained run ID, site, complete file SHA-256 and byte count. Save the command’s JSON output as the handoff file. On the receiving bench, bind the reviewed file to its publisher with `bench --site "$TARGET_SITE" execute ucm_migration_loader.operator.approve_sealed --kwargs '{"handoff_path":"/private/path/handoff.json","publishing_user":"publisher@example.invalid"}'`.
4. With the assigned publisher's token in `UCM_MIGRATION_TOKEN`, run `ucm-push send-publication --site "$TARGET_SITE" --url "$UCM_URL" --publication "$PUBLICATION" --run "$RUN_ID" --progress`. Repeating the same command resumes at the target's byte checkpoint. The operator runs `bench --site "$TARGET_SITE" execute ucm_migration_loader.publication_transport.receive --kwargs '{"run":"reviewed-run-id"}'` with the retained run ID to verify and stage the file.
5. In the Migration Run form select **Apply hidden batches**. Use **Pause after current batch** and **Resume from last receipt** when needed. Review the reconciliation and access findings at **Publication ready**, then select **Activate delegation**. The report records the releasing operator and time. **Discard run** removes a hidden wave in reverse order and retains its receipts. A replacement uses a new snapshot identity.

After activation, use the ordinary UCM editing workflow for approved corrections. In the local rehearsal a caseworker corrected a case name after a follow-up remark had been saved. UCM retained the remark, record ID, editor and migration receipt. The loader refused to discard the activated delegation.

## Target setup

Install the app alongside UCM. Register a worker named `ucm_migration` under the bench's `workers` configuration, with a timeout of 300 seconds, then start a worker for that queue. The loader keeps **UCM Migration Run**, **UCM Migration Batch** and **UCM Migration Held Share**. A Migration Admin approves hand-offs, inspects the audit records and controls loading, discard and activation; a System Manager reads them and gives the role. An enabled API publishing account can send the file of each exact run assigned to it and check its status, and needs no role for that; the Migration Publisher role lets it propose its hand-offs. Each wave uses its own provisioned context UUID on the publishing and target sides.

## Sealing, staging and operating a publication

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

On the receiving bench, `ucm_migration_loader.publication_receiver.stage(path, run)` is a Migration Admin's entry point for a delivered file. It copies to the target site's private migration directory, verifies the retained run ID, locks the receiving context and records a durable receipt. A second submission returns the existing receipt. `Publication staged` means the target has verified the complete file. Its applied cursor stays at zero until a business worker commits records. The sealed HTTPS handoff and both operator workflows are described below.

### Choose write routes on the receiver

Open a staged full-publication **UCM Migration Run** in Desk and choose **Migration → Configure write routes**. The form lists parent DocTypes present in that publication. Each defaults to **Document API**. After the first apply request the plan locks. Desk and CLI apply and resume use the same plan. Open a new batch receipt to see its published DocType and actual write route. Older receipts predate these fields.

Every published parent DocType can use validated SQL. Child records and private attachments use the parent's writer. The worker validates each document, preserves publication order, and runs post-insert hooks before processing dependent records. Person and note batches group multiple parents in one SQL insert. A Person batch flushes preceding rows before validating a link to a person in that same batch. Deferred assignments and shares use the saved route when eligible for release.

### Applying and delivering from the bench

A receiving operator runs `ucm_migration_loader.publication_worker.apply(run, max_batches)` against the private staged copy. The default is one transaction. Run it again from another process to resume. The applied cursor counts committed batches. The worker reports the first deferred access batch when casework is complete. The operator runs `ucm_migration_loader.publication_worker.activate(run)` to check the stored casework and files, refresh and verify sensitivity, restore assignments and shares, and release the context in one transaction. The sealed manifest binds each run to its target site. Application also checks the locked, hidden migration context.

For HTTPS delivery the operator registers the independently obtained run ID, publisher account, complete file SHA-256 and byte length with `ucm_migration_loader.publication_transport.approve(run, publisher, sha256, size)`. The Python client uses `ucm-push send-publication --site SITE --url HTTPS_URL --run RUN --publication FILE` with the publisher token in `UCM_MIGRATION_TOKEN`. It resumes from a durable byte offset. The receiver runs `ucm_migration_loader.publication_transport.receive(run)` to reassemble and check the approved file, then stage it. Transfer, application and activation have separate receipts. The pieces the file arrived in are removed once it is staged.

### Operating and cancelling a sealed publication

On the Migration Run form, select **Apply hidden batches**, **Pause after current batch**, **Resume from last receipt**, **Activate delegation**, or **Discard run** as appropriate. The same commands are available through the existing operator API. Use a worker on the `ucm_migration` queue. The full-publication worker schedules up to 25 batches per queue job and starts no batch after the job's first 120 seconds, so that batches of a thousand records with pictures, which take close to a minute each, stay inside the queue's timeout. Each business batch commits separately. Bounded requests retain an absolute stopping cursor on replay, and a newer command supersedes queued work at the next transaction boundary. The run becomes **Publication ready** after checking the source manifest, committed receipts, UCM option sets, context links, stored rows and deferred access inventory. The reviewer can download that reconciliation from Desk. Activation runs those checks again.

New publications include the MariaDB definitions for 21 owned tables. Previously staged 20-table runs retain their original receipts and scope. A new staging request requires a publication that includes organizations and the sensitive field policy. The signature ignores the order in which indexes were declared and the mutable AUTO_INCREMENT counter. It preserves the columns within each index. `publication_receiver.preflight(path, run)` reports schema differences, context existence and counts of target key collisions before staging. Before its first business write, the worker inventories all incoming identities and resolves references, including audit actors, against the target or the same publication.

Discard processes committed batches in reverse order. Each step locks the context and run, verifies its source receipt, validates record ownership and attachment inventory, and deletes through Frappe. It commits the reduced cursor with the deletes. Restart cleanup from Desk or use `ucm_migration_loader.publication_cleanup.discard(run, max_batches)` on the receiving bench. Original receipts remain available after discard. A new snapshot ID identifies a replacement publication. Activated waves require normal live-data change procedures.

**Framework dependency for cancellation.** This workspace adds the keyword-only `delete_dynamic_links_now` option to `frappe.model.delete_doc.delete_doc`. Deploy that Frappe change with this loader when enabling full-publication cleanup. The [framework patch](dist/frappe-16.52.0-migration-cleanup.patch) was qualified against Frappe 16.52.0 at commit `4515014c87`. On an unpatched checkout, run `git apply --check` against the patch from the Frappe repository before applying it. It runs Frappe's ordinary cleanup of linked assignments, comments and related records in the deletion transaction. A delayed job for a discarded ID could otherwise remove assignments belonging to its re-import. The loader passes the keyword explicitly so an older framework fails before deleting a record.


For a sealed delivery, `ucm-push transfer-status --site SITE --url HTTPS_URL --run RUN` reads the target's byte checkpoint. `send-publication --progress` prints acknowledged bytes on stderr and returns its final JSON receipt on stdout. After staging, `status` reports the business batch cursor and `report` returns the reviewer’s reconciliation. The report includes the published counts, checked business rows, deferred and released access rows, and the SHA-256 digest of the ordered batch sequence. A failed queued operation records its batch and item when available, without copying a person's field values into the run finding. Resolve the reported batch and item to the exact publishing ID with `ucm-push locate-item --site "$TARGET_SITE" --publication "$PUBLICATION" --run "$RUN_ID" --sequence 1 --item 1`.


### Historical shares to future delegations

An open historical share to an inactive receiving delegation creates a **UCM Migration Held Share** receipt, read by Migration Admins and System Managers, when its owner goes live. The published share ID, source run, batch and item position bind it to the original publication. The source report distinguishes live access rows from held receipts. Keep the source site's private staged copy until all of its holds are resolved.

Recipient activation checks the sealed descriptor, reference identities and source batch digest. It evaluates current UCM consent, expiry, record ownership, merged-person status, receiver status and existing shares. An eligible item retains the published ID and status. A consent objection or other eligibility change leaves the receipt in **Review** without creating a Record Share. An integrity, permissions or storage error rolls back the activation transaction. Source and recipient activation are serialized so overlapping go-lives cannot miss a hold. Replay of a completed activation returns its receipt. The source run links to historical share receipts and the receiving run links to shares requiring review.

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

The document route's time comes from its load of the same delegation on the same site a week before the set-based runs; it was not repeated beside them, and it ran in one worker. Carried by the bench's own worker the set-based route took 4.0 min (see Through the queue, below), so read the difference as ten to twenty times, not as the two figures.

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

**Beside live use.** `tests/live_use.py` opens sessions for eight of the site's own users, of four roles in each of two live delegations (196,000 and 98,000 persons), and has each search persons by name, open a person, open a case, list a person's history and save a case of their own, with half a second or more between two requests. The same large delegation was then discarded, staged, loaded and activated (rolled back) by the set-based route while they worked. Over 30 minutes they made 16,008 requests: none failed, and the database counted no row lock wait and no deadlock in any phase.

95th percentile of each request in milliseconds, per phase:

| Phase | Seconds | Open a person | Open a case | List a history | Save a case | Search by name |
|---|---|---|---|---|---|---|
| Quiet, before | 120 | 13 | 11 | 168 | 78 | 2,975 |
| Discard of the earlier load | 564 | 53 | 58 | 502 | 136 | 6,151 |
| Staging the sealed file | 49 | 46 | 41 | 204 | 89 | 4,287 |
| Load: write | 471 | 16 | 13 | 251 | 62 | 3,602 |
| Load: derive | 93 | 13 | 11 | 191 | 57 | 2,892 |
| Load: verify | 222 | 14 | 11 | 195 | 52 | 3,069 |
| Activation, rolled back | 196 | 25 | 24 | 210 | 56 | 3,036 |
| Quiet, after | 60 | 13 | 11 | 165 | 45 | 3,193 |

The write, derive and verify phases left the caseworkers' requests close to their quiet values. The discard disturbed them most: opening a record took about four times as long at the 95th percentile, and the slowest search 9.5 s. The load itself slowed down under the traffic: 13.1 min to **Publication ready** against 10.5 on the quiet site, the discard 9.4 min against 6.2, the activation 193 s against 167.

With `ucm_migration_live_p95_ms` set to 1,000 on the site and the same eight caseworkers at work, the loader's pressure check read the live requests and answered "Paused: live read p95 exceeded 1000 ms"; at 60,000 it let a load go on.

This is a measurement of interference on a development bench: one web process, eight users, a database with a 128 MB buffer pool. It shows that the route takes no lock a caseworker waits for, and in which phases its work on the database is felt; it does not say what a production deployment carries. The name search took about three seconds at the 95th percentile before any load started, which is a matter of the search and not of the loader.

```sh
env/bin/python apps/ucm_migration_loader/tests/live_use.py --site REHEARSAL_SITE --users sites/REHEARSAL_SITE/private/benchmark/users.json --contexts SAH,BOL --caseworkers 8 --run RUN_DIGEST --publication /path/to/publication.sqlite
```

Not covered: a share that UCM's current consent rules no longer allow (none occurred), a delegation larger than 1.25 million rows, and more than a handful of caseworkers.

`tests/set_based_comparison.py` makes this comparison: a row-by-row and field-by-field diff of every table of the publication, and the row count of every table of the site before and after. `tests/test_set_based.py` covers the parts that need no site.

**Through the queue.** `tests/set_based_queue.py` also has `worker`, `activate` (with `--dry-run` to roll back) and `discard`, which ask as Desk does and follow the bench's own worker. Its `rehearse` stages a sealed file, chooses the route and runs the queue's jobs in its own process with a few seconds of budget a job. On the way it checks a pause for live load, a pause asked by the operator, a failure injected into the verification and the resume at the saved batch, the report, an activation refused after a receipt was changed, an activation timed and rolled back, and the discard over several jobs. The run ends discarded. `tests/set_based_queue.py worker` asks for the load as Desk and `ucm-push` do and follows the bench's own worker. `e2e/set_based_route.py` does the same in a browser: it chooses the route in the dialog, applies, and waits for **Publication ready**.

On the same delegation the rehearsal reached **Publication ready** in 134 s, pauses and the injected failure included, over 23 jobs of five seconds, and discarded it in 242 s over 33 jobs. Chosen and applied in Desk and carried by the bench's own worker, which serves every site of the development bench, the run was ready 4.0 min after its first receipt: 181 s of writing (36 s of them for 1,175 sensitivity tags through the documents), 13 s of deriving and 42 s of verifying.

```sh
env/bin/python apps/ucm_migration_loader/tests/set_based_queue.py rehearse --site REHEARSAL_SITE --run RUN_DIGEST --publication /path/to/publication.sqlite
env/bin/python apps/ucm_migration_loader/e2e/set_based_route.py --site REHEARSAL_SITE --run RUN_DIGEST
```

**The run's screens and the metrics hand-over.** `tests/test_run_overview.py` checks without a site what the form and the hand-over are built from: the steps a run's records give in every state, the rows on both sides, the findings with their severity, the metric rows, and the writer of the metrics table. Two scripts check the rest on a rehearsal site that is being served:

```sh
# a load whose hidden rows are changed behind the loader must pause with exactly those findings
env/bin/python apps/ucm_migration_loader/tests/run_overview_site.py findings --site "$TARGET_SITE" --run "$RUN_ID" --publication "$PUBLICATION"
# a file whose record IDs are already on the site is refused, and the refusal is kept as a run
env/bin/python apps/ucm_migration_loader/tests/run_overview_site.py refused --site "$TARGET_SITE" --run "$OTHER_RUN_ID" --publication "$OTHER_PUBLICATION"
# ucm-push quality over TLS, with a publishing account made for the test, into a file and a stand-in table
env/bin/python apps/ucm_migration_loader/tests/run_overview_site.py handover --site "$TARGET_SITE" --run "$RUN_ID" --quality-config quality-db.json
# the list and the three forms in a browser (Playwright and Chromium)
env/bin/python apps/ucm_migration_loader/e2e/run_overview.py --site "$TARGET_SITE" --ready "$READY_RUN" --findings "$RUN_ID" --refused "$OTHER_RUN_ID"
```

`findings` changes one text, three option values and two defaults and removes one child row with direct SQL, after the delegation is written and derived. It avoids the records the verification passes through the documents, because a sampled record that differs stops the verification at that record, which is another path. `handover` needs a MariaDB database standing in for the data quality database; its table must be named `..._rehearsal` and is dropped at the end.

On the 10,000-person delegation (242,856 rows), on the development site, one run each:

| Situation | Result |
|---|---|
| Rows changed behind the loader | The verification paused the run with seven findings and no other: three fields that differ (with batch and item), one child row missing by count and by key, one option value outside its list (HIGH), one default left empty (MEDIUM). 71 metric rows, 11 of them failing. |
| Hand-over of that run | `ucm-push quality` wrote the 71 rows to a CSV file and to the stand-in table, a second call left 71 rows, the run showed the step as done with the account and the table's name, and the run's own time and metrics did not change. A second publishing account was answered HTTP 403. |
| A file sealed from the same delegation while it was loaded | Refused after 10 s: 242,786 record IDs already on the site in 18 tables. A **Refused** run was kept, with no file and no receipt. |
| The refused file staged again after the discard | Checked from the start and staged in the same run, loaded by the bench's own worker and ready after 123 s; 69 metric rows, none failing. |
| In the browser | The list, the form waiting for the decision, the form with findings and the refused form showed these runs, without a page error. |

The times a step list shows are kept from staging onwards by this version; a run staged before it shows its batches' times and no staging time.

**Loading and activating from the form, on both routes.** `run_overview_site.py` also checks a run that came across whole and an activated one, and `e2e/run_overview.py` acts on the form: it asks for the load or takes the go-live decision, then leaves the page alone and waits for the form to show the result by itself.

```sh
env/bin/python apps/ucm_migration_loader/tests/run_overview_site.py ready --site "$TARGET_SITE" --run "$RUN_ID"
env/bin/python apps/ucm_migration_loader/tests/run_overview_site.py activated --site "$TARGET_SITE" --run "$RUN_ID"
env/bin/python apps/ucm_migration_loader/e2e/run_overview.py --site "$TARGET_SITE" --load "$STAGED_RUN" --dashboard "$READY_RUN" --activate "$READY_RUN" --activated "$ACTIVATED_RUN"
```

On three small generator delegations of the development site, one run each. The times of delegations this small say nothing of a route's speed; the larger measurements are above.

| Situation | Result |
|---|---|
| Route dialog, 3 persons (`e2e/set_based_route.py`) | The staged run was given the set-based route in the dialog, applied from the form and ready 5 s later. |
| Go-live decision on the form, same run | **Activate delegation** was confirmed on the form; the form showed the open delegation by itself once the worker had finished, without the page being loaded again. The step list shows who activated, when, and the 0.3 s it took; the context is Active. |
| Hand-over before and after that activation | Done before (52 rows); shown as due again after the activation, whose metrics count the access rows (55 rows); done again after a second `ucm-push quality`. A second account was answered HTTP 403 both times. |
| Document route, 1,000 persons (2,000 rows), staged by this version | Applied from the form, which showed the steps and the result by itself. Stage 0.5 s, write 10.7 s, verify 3.7 s; no derive step; 48 metric rows, none failing, without the set-based checks. Handed over, then activated through the worker: 0.4 s, recorded on the run. |
| Dashboard link | With `ucm_migration_quality_url` set, the form offered the link with the run and the context in it within a minute, and no longer after the setting was removed. |
| An activated delegation with shares (W50BOL, activated earlier) | Its form shows 20 assignments and 13 shares released and 60 shares held; released plus held equals published in the records and in the metrics. It was activated before this version kept the activation's time, so its step shows none. |
| Both routes again after the lint changes to the load code | A 1,000-person delegation each: the document route loaded and verified in 20 s of work, followed on the form's own refresh; the set-based route ready through the worker; the discards of the two earlier runs left no row. |

A discard of a 1,000-person document-route run took 29 to 39 s through the worker. Runs loaded before the batch receipts kept their rows and times show their batches only.

**A job that is killed.** `tests/set_based_kill.py` carries a staged publication through the queue's jobs, each in a process of its own as a worker runs them, and ends the job at seven points. After each one it reads what is left, asks for the load again as an operator resumes it, and lets the next job go on. `--kill process` kills the job's process; `--kill connection` cuts its database connection and lets it run into the error. `worker` kills the job of the bench's own worker instead, at given seconds into the job. The worker itself is never killed.

```sh
env/bin/python apps/ucm_migration_loader/tests/set_based_kill.py points --site "$TARGET_SITE" --run "$RUN_ID" --publication "$PUBLICATION" --kill process --write-call 2 --verify-call 100
env/bin/python apps/ucm_migration_loader/tests/set_based_kill.py points --site "$TARGET_SITE" --run "$OTHER_RUN_ID" --publication "$OTHER_PUBLICATION" --kill connection --write-call 2 --verify-call 100
env/bin/python apps/ucm_migration_loader/tests/set_based_kill.py worker --site "$TARGET_SITE" --run "$THIRD_RUN_ID" --publication "$THIRD_PUBLICATION" --after 20 25 12 20
```

On the 10,000-person delegation (242,786 rows in 222 batches; sealed from the rows the development site held, so without its assignments and shares), one run each. After every ended job the stored rows were exactly those of the batches with a receipt, and the run and its context could be locked again within a second.

| Where the job ended | Process killed | Connection cut |
|---|---|---|
| Write, rows of a transaction inserted and not committed | 4 batches and 3,381 rows kept, the open transaction's rows gone | the same, and the run paused with `apply after committed batch 4: OperationalError` |
| Write, between two transactions | 9 batches, 16,906 rows | the same, paused with the batch in the finding |
| Derive, between two of UCM's rebuilds | all 222 batches written; the derive ran again from its start | the same, paused |
| Verify, after a kept cursor | 99 of 222 batches compared; the next job went on at batch 100 | the same, paused |
| Conclude, rules checked and report not kept | no report, no state change; concluded again | the same, paused |
| The resumed load | ready, no finding, 242,786 rows compared, each once; 244 rules, 4,305 sampled records | the same |
| Activate, before its commit | still ready, context locked and hidden, no access row or share receipt left; the activation then went through (rolled back) | the same, with the failure in the finding |
| Discard, between two chunks | cleaning, 241,986 of 242,786 rows left; the next request removed them all | the same |

Both resumed loads were compared with the undisturbed load of the same delegation by `set_based_comparison.py diff`: 242,786 identical rows, none only on one side, no field differing.

A killed process says nothing, so the run stayed in **Publication applying** with no finding. The form now looks such a run up in the migration queue and says that no job is working on it. A job that loses its connection takes a new one to pause the run with what failed; before this rehearsal it left no trace either.

Through the bench's own worker, the job was killed 20, 25 and 12 seconds into three successive jobs of one load, at 48, 179 and 220 committed batches. Each time the worker stayed up, the queue filed the job as failed, the form said that no job was on the run, and the next request went on; a fourth kill found its job already finished. The load ended ready with every row compared once. For this run the sampled document checks were switched off (`ucm_migration_sample_every`): the bench's long-running worker had not loaded a UCM change made that day and failed on every File record it read, which is the bench's state and not the route's. The delegation was then loaded again with the checks on (4,305 records) and compared: identical.

Not rehearsed: the worker process itself going down (on this bench every process stops with it), the database server stopping, and an activation that had released assignments and shares when it was ended.

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
