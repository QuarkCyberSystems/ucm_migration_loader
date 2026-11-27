/* global ROLE, STATE, FOUND_DIFFERENCES, METHOD, REFUSED, ask, safe, approvable, handoff_summary, context_fields */
// The names and the helpers above are declared in public/js/migration_run_common.js, which Frappe adds to this
// script.

// The state of a run as the migration team reads it, with the colour of what it waits for.
function run_indicator(doc) {
	const filter = "state,=," + doc.state;
	const loaded = doc.received ? Math.floor((100 * (doc.applied || 0)) / doc.received) : 0;
	const states = {
		[STATE.PROPOSED]: [__("Proposed, to approve"), "yellow"],
		[STATE.AWAITING]: [__("Waiting for the file"), "blue"],
		[STATE.STAGED]: [__("Staged"), "gray"],
		[STATE.APPLYING]: [__("Loading, {0} %", [loaded]), "blue"],
		[STATE.PAUSED]:
			doc.worker_phase === FOUND_DIFFERENCES
				? [__("Verification failed"), "red"]
				: [__("Paused"), "orange"],
		[STATE.READY]: [__("Verified, waiting for decision"), "yellow"],
		[STATE.CLEANING]: [__("Discarding"), "orange"],
		[STATE.ACTIVATED]: [__("Activated"), "green"],
		[STATE.DISCARDED]: [__("Discarded"), "gray"],
		[STATE.REJECTED]: [__("Refused"), "red"],
	};
	const [label, color] = states[doc.state] || [__(doc.state), "gray"];
	return [label, color, filter];
}

frappe.listview_settings["UCM Migration Run"] = {
	hide_name_column: true,
	add_fields: ["state", "worker_phase", "received", "applied"],
	formatters: {
		document_count: (value) => format_number(value, null, 0),
	},
	get_indicator: run_indicator,
	onload(listview) {
		if (frappe.user.has_role(ROLE)) {
			listview.page.add_inner_button(__("Approve a hand-off"), () => approve_handoff());
		}
	},
};

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
			...context_fields(null),
		],
		primary_action_label: __("Approve"),
		primary_action: async (values) => {
			const approved = await ask(METHOD + "approve_handoff", {
				handoff,
				publishing_user: values.publishing_user,
				context_code: values.context_code,
				context_name: values.context_name,
			});
			// refused: the dialog stays for another try
			if (approved === REFUSED) return;
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
		// not a hand-off, or not one for this site: the server's message says so
		const found = await ask(METHOD + "inspect_handoff", { handoff }, false);
		if (found === REFUSED) return;
		const fingerprint = [
			__("Fingerprint"),
			`<code style="word-break: break-all">${safe(found.sha256)}</code>`,
		];
		dialog.fields_dict.summary.$wrapper.html(handoff_summary(found, fingerprint));
		const missing = !found.context.exists;
		dialog.set_df_property("context_section", "hidden", missing ? 0 : 1);
		dialog.set_value("context_code", missing ? (found.delegation || {}).code || "" : "");
		dialog.set_value("context_name", missing ? (found.delegation || {}).name || "" : "");
		dialog.get_primary_btn().prop("disabled", !approvable(found));
	});
	dialog.show();
}
