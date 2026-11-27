"""The shared catalogs a delegation refers to, and the activities a publication cannot carry."""

ORGANIZATION_CATALOGS = ("Political Power", "Political Power Precision", "Global Actor")

CATALOGS = (
	"Context",
	"Protection Department",
	"Field Clearance",
	"General Objective",
	"Specific Objective",
	"Place Type",
	"Activity Precision",
	"Subtarget Population",
	"Service Type",
	"Activity Type",
	"Sensitivity Tag",
	*ORGANIZATION_CATALOGS,
	"Context Profile Exemption",
)

MISSING_PLACE = """from tabActivity a join `tabActivity Type` t on t.name=a.activity_type_tech_id
    where a.context_tech_id=%s and t.activity_type_enabled_place=1
    and t.activity_type_place_required=1 and nullif(cast(a.activity_place_tech_id as char),'') is null"""
