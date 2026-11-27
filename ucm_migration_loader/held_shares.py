"""Keep historical shares private until the receiving delegation's release transaction."""

import json
from collections import Counter
from hashlib import sha256
from itertools import groupby
from operator import attrgetter

import frappe

from . import constants, runs
from .constants import HELD
from .contract import digest
from .frozen import FORMAT, open_sealed, reference_digest
from .publication_receiver import local_publication, same_target
from .publication_shares import IneligibleShare, check_share


def lock_activation():
	# Two delegations may point shares at each other. Order the two activation
	# transactions before either locks a context or discovers the other's receipts.
	frappe.db.get_value("DocType", constants.RUN, "name", for_update=True)


def hold(record, raw, sequence, position):
	frappe.get_doc(
		{
			"doctype": HELD,
			"migration_run": record.name,
			"owner_context": record.context_id,
			"receiver_context": raw["share_to_context_tech_id"],
			"sequence_number": sequence,
			"item_number": position,
			"state": "Held",
		}
	).insert(set_name=raw["name"], ignore_permissions=True)


def summary(run):
	return dict(
		frappe.db.sql(
			"""select state,count(*) from `tabUCM Migration Held Share`
        where migration_run=%s group by state""",
			(run,),
		)
	)


def release_incoming(ctx, receiving_run):
	from .publication_worker import release_item

	held = frappe.db.get_values(
		HELD,
		{"receiver_context": ctx.name, "state": "Held"},
		["name", "migration_run", "owner_context", "sequence_number", "item_number"],
		as_dict=True,
		order_by="migration_run asc, sequence_number asc, item_number asc",
		for_update=True,
	)
	outcomes = Counter()
	for run, entries in groupby(held, key=attrgetter("migration_run")):
		source = runs.record(run)
		if source.state != constants.ACTIVATED:
			raise ValueError("Held share source run has not been activated")
		with open_sealed(local_publication(run)) as db:
			(descriptor,) = db.execute("select manifest from descriptor").fetchall()
			manifest = json.loads(descriptor[0])
			# Only these recorded share batches are consumed. Their target receipts were
			# created after the original full verification, including its ordered hash chain.
			if (
				manifest.get("format") != FORMAT
				or digest(manifest) != run
				or reference_digest(db) != manifest["reference_sha256"]
			):
				raise ValueError("Held share source descriptor or reference identities differ")
			if not same_target(manifest, source.context_id, source):
				raise ValueError("Held share source manifest differs")
			cache = {}
			for seq, batch_entries in groupby(entries, key=attrgetter("sequence_number")):
				sha, payload = db.execute(
					"select sha,payload from batches where sequence=?", (seq,)
				).fetchone()
				receipt = frappe.get_doc(constants.BATCH, f"{run}-{seq}", for_update=True)
				if (receipt.migration_run, receipt.sequence_number, receipt.digest) != (
					run,
					seq,
					sha,
				):
					raise ValueError("Held share batch differs from committed receipt")
				if sha256(payload).hexdigest() != sha:
					raise ValueError("Held share payload differs from the committed digest")
				items = json.loads(payload)
				for entry in batch_entries:
					if not 1 <= entry.item_number <= len(items):
						raise ValueError("Invalid held share position")
					item = items[entry.item_number - 1]
					raw = item["row"]
					if (
						item["doctype"],
						raw["name"],
						raw["context_tech_id"],
						entry.owner_context,
						raw["share_to_context_tech_id"],
					) != (
						"Record Share",
						entry.name,
						source.context_id,
						source.context_id,
						ctx.name,
					):
						raise ValueError("Held share identity differs from the publication")
					finding = None
					try:
						if (
							frappe.db.get_value(
								"Context",
								source.context_id,
								"context_status",
								for_update=True,
							)
							!= "Active"
						):
							raise IneligibleShare("Owning delegation is no longer active")
						if check_share(raw, source.context_id) != "active_receiver":
							raise IneligibleShare("Historical share is no longer eligible for release")
						if frappe.db.get_value(
							"Record Share", raw["name"], "name", for_update=True
						) or frappe.db.get_value(
							"Record Share",
							{
								"share_doctype": raw["share_doctype"],
								"share_name": raw["share_name"],
								"share_to_context_tech_id": ctx.name,
								"share_status": ["in", ["Accepted", "Pending"]],
							},
							"name",
							for_update=True,
						):
							raise IneligibleShare("A live share already exists")
					except IneligibleShare as exc:
						finding = str(exc)
					else:
						release_item(
							db,
							cache,
							item,
							source.context_id,
							route=receipt.write_route or "Document API",
						)
					state = "Review" if finding else "Released"
					frappe.db.set_value(
						HELD,
						entry.name,
						{
							"state": state,
							"finding": finding,
							"release_run": receiving_run,
						},
					)
					outcomes[state] += 1
	return dict(outcomes)
