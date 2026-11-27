app_name = "ucm_migration_loader"
app_title = "UCM Migration Loader"
app_publisher = "Quark Cyber Systems"
app_description = "Rehearse and import versioned publishing snapshots into UCM"
app_email = "vivek@quarkcs.com"
required_apps = ["ucm"]

doc_events = {
	"Context": {"validate": "ucm_migration_loader.application.guard_context"},
	"Record Share": {"validate": "ucm_migration_loader.application.guard_share"},
}

before_request = ["ucm_migration_loader.migration_load.before_request"]
after_request = ["ucm_migration_loader.migration_load.after_request"]
