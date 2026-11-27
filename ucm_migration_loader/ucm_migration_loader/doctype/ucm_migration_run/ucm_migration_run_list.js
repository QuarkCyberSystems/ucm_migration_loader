// The state of a run as the migration team reads it, with the colour of what it waits for.
function run_indicator(doc) {
	const filter = "state,=," + doc.state;
	const loaded = doc.received ? Math.floor((100 * (doc.applied || 0)) / doc.received) : 0;
	const failed = (doc.finding || "").startsWith("Verification found");
	const states = {
		"Publication staged": [__("Staged"), "gray"],
		"Publication applying": [__("Loading, {0} %", [loaded]), "blue"],
		"Publication paused": failed
			? [__("Verification failed"), "red"]
			: [__("Paused"), "orange"],
		"Publication ready": [__("Verified, waiting for decision"), "yellow"],
		"Publication cleaning": [__("Discarding"), "orange"],
		Activated: [__("Activated"), "green"],
		Discarded: [__("Discarded"), "gray"],
		Rejected: [__("Refused"), "red"],
	};
	const [label, color] = states[doc.state] || [__(doc.state), "gray"];
	return [label, color, filter];
}

frappe.listview_settings["UCM Migration Run"] = {
	hide_name_column: true,
	add_fields: ["state", "finding", "received", "applied"],
	formatters: {
		document_count: (value) => format_number(value, null, 0),
	},
	get_indicator: run_indicator,
};
