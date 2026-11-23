from frappe.model.document import Document

from ucm_migration_loader.display_ids import receipt_id


class UCMMigrationBatch(Document):
    def before_insert(self):
        self.display_id = receipt_id(self.migration_run, self.sequence_number)
