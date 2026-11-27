import frappe
from frappe.model.document import Document

from ucm_migration_loader.display_ids import new_run_id


class UCMMigrationRun(Document):
	def before_insert(self):
		self.display_id = new_run_id()


@frappe.whitelist()
def overview(run: str):
	"""The steps, the rows on both sides and the findings that the run's form shows."""
	from ucm_migration_loader.ucm_migration_loader import run_overview

	frappe.only_for("System Manager")
	return run_overview.build(frappe.get_doc("UCM Migration Run", run))
