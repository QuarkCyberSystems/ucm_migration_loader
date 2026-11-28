# For scripts: the detailed commands and the bench

The detailed commands take every value as an option, for scripts and for cases [the short way](../README.md#how-a-delegation-moves) does not cover: `ucm-push seal`, `propose` and `send` do the same in fewer steps, from a settings file. Every command is listed in the technical reference, under [All CLI commands](reference.md#all-cli-commands). The following example uses Bash, `jq`, and a synthetic context UUID. Replace the example host, UUID, paths and accounts with the values for the delegation. Use a new private working directory for each snapshot.

## 1. Prepare the receiving context

A hand-off made by `ucm-push seal` names its delegation, and its approval makes the context where the site has none, locked and hidden. The detailed commands below write a hand-off without that name: approve it in the Desk, where the code and the name are typed, or make the context first. It is an empty **Context** with the same UUID and code as the published delegation, with **Context Status** set to **Locked** and **Context Discoverability** to **Hidden**. For example, on the receiving bench:

```sh
bench --site "$TARGET_SITE" console
```

```python
context_id = "b9d729a1-eeb8-45f5-93b7-12ca51bc447b"
ctx = frappe.get_doc(
	{
		"doctype": "Context",
		"context_code": "DEMO01",
		"context_name": "Synthetic delegation",
		"context_status": "Locked",
		"context_discoverability": "Hidden",
	}
).insert(set_name=context_id)
assert ctx.name == context_id
frappe.db.commit()
```

## 2. Inspect and prepare the publication on the publishing host

`source.json` contains PyMySQL connection arguments for a read-only account ([sample](../samples/source.json), [every key explained](../samples/README.md)). Store the actual credential outside the repo in a private file. A local MariaDB publisher can use `unix_socket` in place of the host and port.

```json
{"host":"publisher.example.org","port":3306,"database":"ucm_publication","user":"migration_reader","password":"<read-only database password>"}
```

```bash
umask 077
export TARGET_SITE=ucm.example.org
export UCM_URL=https://ucm.example.org
export CONTEXT_ID=b9d729a1-eeb8-45f5-93b7-12ca51bc447b
export SNAPSHOT=DEMO01-publication-01
export PUBLISH_DB_CONFIG=/srv/migration/credentials/source.json
export PRIVATE_FILES=/srv/publication/private/files
mkdir -p "$HOME/ucm-publications/$SNAPSHOT"
cd "$HOME/ucm-publications/$SNAPSHOT"
export PUBLICATION="$PWD/delegation.sqlite"

src=(--site "$TARGET_SITE" --context "$CONTEXT_ID" --snapshot "$SNAPSHOT" --source-config "$PUBLISH_DB_CONFIG")
ucm-push source-inventory "${src[@]}" > inventory.json
ucm-push upload-order "${src[@]}" --batch-size 1000 > activity-order.json
ucm-push prepare "${src[@]}" --private-files "$PRIVATE_FILES" \
  --publication-out "$PUBLICATION" --findings-out "$PWD/findings.jsonl" \
  --batch-size 1000 --batch-bytes 4194304 > prepared.json
export RUN_ID="$(jq -er '.run' prepared.json)"
ucm-push check-publication --site "$TARGET_SITE" --publication "$PUBLICATION" \
  --run "$RUN_ID" > handoff.json
jq '{run,site,snapshot,batches,counts,file_bytes,file_sha256}' handoff.json
```

## 3. Approve, transfer and stage

Deliver the independently reviewed `handoff.json` to the receiving operator. A Migration Admin approves it for one enabled publishing account, in the Desk ([The short way](../README.md#how-a-delegation-moves)) or on the receiving bench. The approval binds the file's fingerprint and size to that account, and the publication becomes a run that waits for its file:

```bash
bench --site "$TARGET_SITE" ucm-migration approve /srv/approved/DEMO01/handoff.json --publisher publisher@example.org
```

On the publishing host, load the publisher's Frappe API key and secret from your credential store as `UCM_MIGRATION_TOKEN` in `key:secret` format. For a private TLS authority, set `REQUESTS_CA_BUNDLE` to its CA bundle.

```bash
remote=(--site "$TARGET_SITE" --url "$UCM_URL" --run "$RUN_ID")
ucm-push transfer-status "${remote[@]}"
ucm-push send-publication "${remote[@]}" --publication "$PUBLICATION" --progress
```

This transfers one sealed file in resumable 512 KiB chunks. The site writes them into one file beside its staged files and notes how far that file is safely on the disk; staging renames it, so the site holds the publication once and needs free space for its size only. The staging job is given an hour, and three minutes more for each gigabyte beyond twenty. The site's worker stages the file by itself when the whole of it has arrived. To ask for the staging from the receiving bench instead, and to set `RUN_ID` for the commands that follow:

```bash
bench --site "$TARGET_SITE" ucm-migration stage /srv/approved/DEMO01/handoff.json
export RUN_ID="$(jq -er '.run' /srv/approved/DEMO01/handoff.json)"
```

The run is then **Staged**: the site has verified the file and recorded its batch count, and nothing is written. Desk shows a short ID such as `MIG-0028`. The commands use the run's whole identifier, in `RUN_ID`.

What the site must hold before a load, and what the sealing writes and refuses, are in the technical reference: [What the site must hold before a load](reference.md#what-the-site-must-hold-before-a-load) and [What the sealing writes and refuses](reference.md#what-the-sealing-writes-and-refuses).

## Without a browser: the bench's commands

A hand-off is approved and staged in the Desk. The same two steps can be taken from the receiving bench, for a site without a browser at hand and for scripts:

```sh
bench --site ucm.example.org ucm-migration approve /srv/approved/DEMO01/handoff.json --publisher publisher@example.org
bench --site ucm.example.org ucm-migration stage /srv/approved/DEMO01/handoff.json
```

## The form of the answers

At a terminal the commands answer in sentences. Redirected or piped, they answer in JSON, as scripts expect; `--json` and `--text` choose outright. `tests/operator_flow.py` runs this whole way with the real commands over TLS; with `--desk` the hand-off is proposed with `ucm-push propose`, approved in a browser with its check code (`e2e/site_operator.py`), and staged by the worker.
