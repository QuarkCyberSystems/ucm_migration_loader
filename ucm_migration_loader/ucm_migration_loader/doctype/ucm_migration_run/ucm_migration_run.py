from frappe.model.document import Document

from ucm_migration_loader.display_ids import new_run_id


class UCMMigrationRun(Document):
    def before_insert(self):
        self.display_id = new_run_id()
