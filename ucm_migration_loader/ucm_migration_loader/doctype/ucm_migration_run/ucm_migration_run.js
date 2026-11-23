frappe.ui.form.on("UCM Migration Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		const state = frm.doc.state;
		show_batch_progress(frm);
		frm.add_custom_button(__("View batches"), () =>
			frappe.set_route("List", "UCM Migration Batch", { migration_run: frm.doc.name })
		);
		const group = __("Migration");
		frm.add_custom_button(__("Refresh progress"), () => frm.reload_doc(), group);

		function action(label, method, confirmation) {
			const submit = async () => {
				await frappe.call({
					method: "ucm_migration_loader.api." + method,
					args: { run: frm.doc.name },
					freeze: true,
				});
				await frm.reload_doc();
			};
			frm.add_custom_button(__(label), () => {
				if (confirmation) return frappe.confirm(__(confirmation), submit);
				return submit();
			}, group);
		}

		if (state === "Publication staged" && !frm.doc.dispatch_id && !frm.doc.applied) {
            frm.add_custom_button(__("Configure write routes"), () => configure_write_routes(frm), group);
        }
        if (state === "Staged") action("Verify", "verify");
		if (["Verified", "Publication staged"].includes(state)) action("Apply hidden batches", "apply");
		if (["Applying", "Publication applying"].includes(state)) {
			action("Pause after current batch", "pause");
			action("Resume from last receipt", "apply");
		}
		if (["Paused", "Publication paused"].includes(state)) action("Resume from last receipt", "apply");
		if (["Ready", "Publication ready"].includes(state)) {
			action("Activate delegation", "activate", "Open this delegation to its authorized users after reviewing reconciliation?");
		}
		if (["Cleaning", "Publication cleaning"].includes(state)) {
			action("Resume cleanup", "discard");
		} else if (!["Activated", "Discarded"].includes(state)) {
			action("Discard run", "discard", "Discard this run and remove its applied records? The audit receipts will be retained.");
		}
		if (frm.doc.reconciliation_json) {
            const report = JSON.parse(frm.doc.reconciliation_json);
            if (report.shares_held_at_activation > 0) {
                frm.add_custom_button(__("Historical share receipts"), () =>
                    frappe.set_route("List", "UCM Migration Held Share", { migration_run: frm.doc.name }), group);
            }
            if (report.inbound_share_outcomes?.Review > 0) {
                frm.add_custom_button(__("Shares requiring review"), () =>
                    frappe.set_route("List", "UCM Migration Held Share", { release_run: frm.doc.name, state: "Review" }), group);
            }
			frm.add_custom_button(__("Download report"), async () => {
				const { message } = await frappe.call({
					method: "ucm_migration_loader.api.report",
					args: { run: frm.doc.name },
				});
				const url = URL.createObjectURL(new Blob([JSON.stringify(message, null, 2)], { type: "application/json" }));
				const link = document.createElement("a");
				link.href = url;
				link.download = "ucm-migration-" + frm.doc.name + ".json";
				link.click();
				setTimeout(() => URL.revokeObjectURL(url), 1000);
			}, group);
		}
	},
});

function show_batch_progress(frm) {
	let batches;
	try {
		batches = JSON.parse(frm.doc.manifest_json || "{}").batches;
	} catch {
		return;
	}
	const total = Array.isArray(batches) ? batches.length : batches;
	if (!Number.isSafeInteger(total) || total < 1) return;

	const applied = Number(frm.doc.applied || 0);
	const remaining = total - applied;
	const counts = [applied, total, remaining].map(n => n.toLocaleString());
	let message = __("{0} of {1} batches finished · {2} remaining", counts);
	if (frm.doc.state === "Receiving") {
		message += " · " + __("{0} received", [Number(frm.doc.received || 0).toLocaleString()]);
	} else if (frm.doc.state === "Publication ready" && remaining > 0) {
		message += " · " + __("Remaining batches run at activation.");
	} else if (["Cleaning", "Publication cleaning", "Discarded"].includes(frm.doc.state)) {
		message = __("{0} of {1} batches currently applied", counts);
	}
	frm.dashboard.show_progress(__("Batch progress"), Math.floor(100 * applied / total), message);
}

async function configure_write_routes(frm) {
    const method = "ucm_migration_loader.write_routes.";
    const args = {run: frm.doc.name};
    const {message: rows} = await frappe.call({method: method + "options", args});
    const dialog = new frappe.ui.Dialog({
        title: __("Configure write routes"),
        fields: rows.map((row, i) => ({
            fieldname: "route_" + i,
            label: row.doctype,
            fieldtype: "Select",
            options: row.sql_allowed ? ["Document API", "Validated bulk SQL"] : ["Document API"],
            default: row.sql_allowed ? row.selected : "Document API",
            read_only: !row.sql_allowed,
            description: row.sql_allowed ? undefined : __("Bulk SQL is unavailable for this DocType on this site."),
        })),
        primary_action_label: __("Save routes"),
        async primary_action(values) {
            const routes = Object.fromEntries(rows.map((row, i) =>
                [row.doctype, row.sql_allowed ? values["route_" + i] : "Document API"]));
            await frappe.call({method: method + "configure", args: {...args, routes}, freeze: true});
            dialog.hide();
            await frm.reload_doc();
        },
    });
    dialog.show();
}
