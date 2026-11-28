"""Keep historical shares private until the receiving delegation's release transaction."""

import json
from collections import Counter
from itertools import groupby
from operator import attrgetter

import frappe

from . import constants, runs
from .constants import HELD
from .publication_receiver import same_target
from .publication_shares import IneligibleShare, check_share


def lock_activation():
	# Two delegations may point shares at each other. Order the two activation
	# transactions before either locks a context or discovers the other's receipts.
	frappe.db.get_value("DocType", constants.RUN, "name", for_update=True)


def hold(record, raw, sequence, position, db):
	"""Keep a share to a delegation that is not open yet, with what its release needs: the published row and
	what the publication says of the references it names. The run's file is removed when the run is activated,
	so nothing is read from it afterwards. The field that keeps them is at a level no role reads."""
	from .publication_worker import reference_attributes, references

	kept = {
		"row": raw,
		"references": [
			[master, value, reference_attributes(db, master, value)]
			for master, value in references("Record Share", raw)
		],
	}
	frappe.get_doc(
		{
			"doctype": HELD,
			"migration_run": record.name,
			"owner_context": record.context_id,
			"receiver_context": raw["share_to_context_tech_id"],
			"sequence_number": sequence,
			"item_number": position,
			"state": "Held",
			"share_json": json.dumps(kept),
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
	"""Release the shares other delegations hold for this one, from what their runs kept at activation."""
	from .publication_worker import release_item, resolve_attributes

	held = frappe.db.get_values(
		HELD,
		{"receiver_context": ctx.name, "state": "Held"},
		["name", "migration_run", "owner_context", "sequence_number", "item_number", "share_json"],
		as_dict=True,
		order_by="migration_run asc, sequence_number asc, item_number asc",
		for_update=True,
	)
	outcomes = Counter()
	for run, entries in groupby(held, key=attrgetter("migration_run")):
		source = runs.record(run)
		if source.state != constants.ACTIVATED:
			raise ValueError("Held share source run has not been activated")
		if not same_target(json.loads(source.manifest_json), source.context_id, source):
			raise ValueError("Held share source manifest differs")
		cache = {}
		for seq, batch_entries in groupby(entries, key=attrgetter("sequence_number")):
			# the receipt of the batch the shares came in, committed with the activation that held them
			receipt = frappe.get_doc(constants.BATCH, f"{run}-{seq}", for_update=True)
			if (receipt.migration_run, receipt.sequence_number) != (run, seq):
				raise ValueError("Held share batch differs from committed receipt")
			for entry in batch_entries:
				if not entry.share_json:
					raise ValueError("Held share was recorded without its row")
				kept = json.loads(entry.share_json)
				raw = kept["row"]
				if (
					raw["name"],
					raw["context_tech_id"],
					entry.owner_context,
					raw["share_to_context_tech_id"],
				) != (
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
					for master, value, attributes in kept["references"]:
						if (master, value) not in cache:
							resolve_attributes(cache, master, value, attributes)
					release_item(
						None,
						cache,
						{"doctype": "Record Share", "row": raw},
						source.context_id,
						route=receipt.write_route or "Document API",
					)
				# released or left for review, its copy has served
				frappe.db.set_value(
					HELD,
					entry.name,
					{
						"state": "Review" if finding else "Released",
						"finding": finding,
						"release_run": receiving_run,
						"share_json": None,
					},
				)
				outcomes["Review" if finding else "Released"] += 1
	return dict(outcomes)
