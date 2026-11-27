import frappe

from ucm_migration_loader.display_ids import new_run_id, receipt_id


def execute():
	run_dt = "UCM Migration Run"
	for name in frappe.get_all(
		run_dt,
		filters={"display_id": ["is", "not set"]},
		order_by="creation asc, name asc",
		pluck="name",
	):
		frappe.db.set_value(run_dt, name, "display_id", new_run_id(), update_modified=False)
	for dt in ("UCM Migration Batch", "UCM Migration Held Share"):
		fields = ["name", "migration_run", "sequence_number"]
		if dt == "UCM Migration Held Share":
			fields.append("item_number")
		while rows := frappe.get_all(
			dt,
			filters={"display_id": ["is", "not set"]},
			fields=fields,
			limit=500,
			order_by="name asc",
		):
			frappe.db.bulk_update(
				dt,
				{
					r.name: {
						"display_id": receipt_id(r.migration_run, r.sequence_number, r.get("item_number"))
					}
					for r in rows
				},
				update_modified=False,
			)
