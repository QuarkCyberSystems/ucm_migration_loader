// The state of a run as the migration team reads it, with the colour of what it waits for.
function run_indicator(doc) {
	const filter = "state,=," + doc.state;
	const loaded = doc.received ? Math.floor((100 * (doc.applied || 0)) / doc.received) : 0;
	const failed = (doc.finding || "").startsWith("Verification found");
	const states = {
		Proposed: [__("Proposed, to approve"), "yellow"],
		"Awaiting publication": [__("Waiting for the file"), "blue"],
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
	onload(listview) {
		if (frappe.user.has_role("Migration Admin")) {
			listview.page.add_inner_button(__("Approve a hand-off"), () => approve_handoff());
		}
	},
};

const megabytes = (bytes) =>
	(Number(bytes || 0) / 1e6).toLocaleString(undefined, {
		maximumFractionDigits: 1,
		minimumFractionDigits: 1,
	}) + " MB";
const METHOD =
	"ucm_migration_loader.ucm_migration_loader.doctype.ucm_migration_run.ucm_migration_run.";

// What a hand-off says, for the person who approves it: the delegation, its size, the file's fingerprint, and
// whether the site has a context ready for it.
function handoff_summary(found) {
	const safe = (text) => frappe.utils.escape_html(String(text ?? ""));
	const about = found.delegation || {};
	const named = found.context.exists
		? `${found.context.name} (${found.context.code})`
		: about.code
		? `${about.name} (${about.code})`
		: found.context.id;
	let context, color;
	if (found.known && found.known.state !== "Awaiting publication") {
		[context, color] = [
			__("This publication is already on the site as {0}.", [found.known.display_id]),
			"text-danger",
		];
	} else if (found.context.problem) {
		[context, color] = [found.context.problem, "text-danger"];
	} else if (found.context.exists) {
		[context, color] = [
			__("Locked, hidden and empty: ready for this publication."),
			"text-success",
		];
	} else {
		[context, color] = [
			__("This site has no context for it yet. It is made below, locked and hidden."),
			"text-muted",
		];
	}
	const rows = [
		[__("Delegation"), safe(named)],
		[__("Snapshot"), safe(found.snapshot)],
		[
			__("Rows"),
			__("{0} in {1} batches", [
				Number(found.rows).toLocaleString(),
				Number(found.batches).toLocaleString(),
			]),
		],
		[__("Sealed file"), megabytes(found.bytes)],
		[__("Fingerprint"), `<code style="word-break: break-all">${safe(found.sha256)}</code>`],
		[__("Context"), `<span class="${color}">${safe(context)}</span>`],
	];
	return `<table class="table table-borderless table-sm" style="margin-top: var(--margin-md)">${rows
		.map(
			([label, value]) =>
				`<tr><td class="text-muted" style="width: 8rem">${label}</td><td>${value}</td></tr>`
		)
		.join("")}</table>`;
}

// A Migration Admin approves a reviewed hand-off for the account that will send its file. The file itself never
// passes through the browser: only the hand-off, which holds counts and fingerprints.
function approve_handoff() {
	let handoff = null;
	const dialog = new frappe.ui.Dialog({
		title: __("Approve a hand-off"),
		fields: [
			{
				fieldname: "file",
				fieldtype: "HTML",
				options: `<label class="control-label">${__(
					"The handoff.json of the sealed publication"
				)}</label>
					<input type="file" accept=".json,application/json" class="form-control ucm-handoff-file">`,
			},
			{ fieldname: "summary", fieldtype: "HTML" },
			{
				fieldname: "publishing_user",
				fieldtype: "Link",
				options: "User",
				label: __("Publishing account"),
				reqd: 1,
				description: __(
					"The account that will send the sealed file. It can read this run, and no other."
				),
			},
			{
				fieldname: "context_section",
				fieldtype: "Section Break",
				label: __("The delegation's context"),
				hidden: 1,
			},
			{ fieldname: "context_code", fieldtype: "Data", label: __("Code") },
			{ fieldname: "context_column", fieldtype: "Column Break" },
			{ fieldname: "context_name", fieldtype: "Data", label: __("Name") },
		],
		primary_action_label: __("Approve"),
		primary_action: async (values) => {
			let approved;
			try {
				({ message: approved } = await frappe.call({
					method: METHOD + "approve_handoff",
					args: {
						handoff,
						publishing_user: values.publishing_user,
						context_code: values.context_code,
						context_name: values.context_name,
					},
					freeze: true,
				}));
			} catch {
				// refused, and said so by the server's message: the dialog stays for another try
				return;
			}
			dialog.hide();
			frappe.show_alert({
				message: __("Approved as {0}. It is staged when its file has arrived.", [
					approved.display_id,
				]),
				indicator: "green",
			});
			frappe.set_route("Form", "UCM Migration Run", approved.run);
		},
	});
	dialog.get_primary_btn().prop("disabled", true);
	dialog.$wrapper.find(".ucm-handoff-file").on("change", async (event) => {
		const [chosen] = event.target.files;
		dialog.get_primary_btn().prop("disabled", true);
		dialog.fields_dict.summary.$wrapper.empty();
		if (!chosen) return;
		handoff = await chosen.text();
		let found;
		try {
			({ message: found } = await frappe.call({
				method: METHOD + "inspect_handoff",
				args: { handoff },
			}));
		} catch {
			// not a hand-off, or not one for this site: the server's message says so
			return;
		}
		dialog.fields_dict.summary.$wrapper.html(handoff_summary(found));
		const missing = !found.context.exists;
		dialog.set_df_property("context_section", "hidden", missing ? 0 : 1);
		dialog.set_value("context_code", missing ? (found.delegation || {}).code || "" : "");
		dialog.set_value("context_name", missing ? (found.delegation || {}).name || "" : "");
		const blocked =
			found.context.problem || (found.known && found.known.state !== "Awaiting publication");
		dialog.get_primary_btn().prop("disabled", Boolean(blocked));
	});
	dialog.show();
}
