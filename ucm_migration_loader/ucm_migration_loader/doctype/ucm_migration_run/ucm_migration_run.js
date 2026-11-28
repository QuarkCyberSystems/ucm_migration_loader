/* global ROLE, STATE, SET_BASED, BY_DOCTYPE, METHOD, REFUSED, ask, number, safe, megabytes, approvable,
   handoff_summary, context_fields */
// The names and the helpers above are declared in public/js/migration_run_common.js, which Frappe adds to this
// script.

frappe.ui.form.on("UCM Migration Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		// a run is changed by its actions only, never by editing its fields
		frm.disable_save();
		const state = frm.doc.state;
		if (![STATE.REJECTED, STATE.AWAITING, STATE.PROPOSED].includes(state))
			show_batch_progress(frm);
		show_write_route(frm);
		show_overview(frm);
		frm.add_custom_button(__("View batches"), () =>
			frappe.set_route("List", "UCM Migration Batch", { migration_run: frm.doc.name })
		);
		const group = __("Migration");
		frm.add_custom_button(__("Refresh progress"), () => frm.reload_doc(), group);
		show_share_lists(frm, group);
		// the server asks the same of every action: who only reads a run is offered none
		if (frappe.user.has_role(ROLE)) show_actions(frm, group);
	},
});

// The actions a run's state allows. The labels and the questions are given translated.
function show_actions(frm, group) {
	const state = frm.doc.state;

	function action(label, method, question) {
		const submit = async () => {
			const answer = await ask("ucm_migration_loader.api." + method, { run: frm.doc.name });
			// refused or done, the run may have moved: show where it stands
			await frm.reload_doc();
			if (answer !== REFUSED && method === "activate") follow_activation(frm);
		};
		const run = () => (question ? frappe.confirm(question, submit) : submit());
		frm.add_custom_button(label, run, group);
		return run;
	}

	if (state === STATE.STAGED) {
		// the route is chosen before the first batch is written
		if (!frm.doc.dispatch_id && !frm.doc.applied) {
			frm.add_custom_button(
				__("Configure write routes"),
				() => configure_write_routes(frm),
				group
			);
		}
		action(__("Apply hidden batches"), "apply");
	}
	if (state === STATE.APPLYING) {
		action(__("Pause after current batch"), "pause");
		action(__("Resume from last receipt"), "apply");
	}
	if (state === STATE.PAUSED) action(__("Resume from last receipt"), "apply");
	if (state === STATE.READY) {
		const activate = action(
			__("Activate delegation"),
			"activate",
			__("Open this delegation to its authorized users after reviewing reconciliation?")
		);
		// the decision the run waits for, beside the menu that holds every action
		frm.add_custom_button(__("Activate delegation"), activate).addClass("btn-primary");
	}
	if (state === STATE.CLEANING) {
		action(__("Resume cleanup"), "discard");
	} else if (state === STATE.PROPOSED) {
		action(
			__("Decline"),
			"discard",
			__("Decline this proposed hand-off? No file will be accepted for it.")
		);
		// the decision this run waits for
		frm.add_custom_button(__("Approve hand-off"), () => approve_proposed(frm)).addClass(
			"btn-primary"
		);
	} else if (state === STATE.AWAITING) {
		action(
			__("Withdraw approval"),
			"discard",
			__(
				"Withdraw the approval of this publication? What has arrived of its file is removed."
			)
		);
	} else if (state === STATE.REJECTED) {
		// the refused run stays as the record; what it still holds on the site can go
		if (!file_removed(frm))
			action(
				__("Remove arrived file"),
				"discard",
				__(
					"Remove the file that arrived for this refused run? It cannot be staged afterwards: a corrected publication is sealed and approved anew."
				)
			);
	} else if (![STATE.ACTIVATED, STATE.DISCARDED].includes(state)) {
		action(
			__("Discard run"),
			"discard",
			__(
				"Discard this run and remove its applied records? The audit receipts will be retained."
			)
		);
	}
	if ([STATE.AWAITING, STATE.REJECTED].includes(state) && !file_removed(frm)) {
		// staging follows the file's arrival by itself; this asks for it again, after a failure or a refusal
		frm.add_custom_button(__("Stage now"), () => stage_now(frm), group);
	}
	if (frm.doc.reconciliation_json) {
		frm.add_custom_button(__("Download report"), () => download_report(frm), group);
	}
}

// Whether the file that arrived for a refused run was removed from the site since.
function file_removed(frm) {
	return Boolean(JSON.parse(frm.doc.approval_json || "{}").removed_on);
}

// The lists of the shares this run held back, for who reads the run.
function show_share_lists(frm, group) {
	if (!frm.doc.reconciliation_json) return;
	const report = JSON.parse(frm.doc.reconciliation_json);
	if (report.shares_held_at_activation > 0) {
		frm.add_custom_button(
			__("Historical share receipts"),
			() =>
				frappe.set_route("List", "UCM Migration Held Share", {
					migration_run: frm.doc.name,
				}),
			group
		);
	}
	if (report.inbound_share_outcomes?.Review > 0) {
		frm.add_custom_button(
			__("Shares requiring review"),
			() =>
				frappe.set_route("List", "UCM Migration Held Share", {
					release_run: frm.doc.name,
					state: "Review",
				}),
			group
		);
	}
}

async function download_report(frm) {
	const report = await ask("ucm_migration_loader.api.report", { run: frm.doc.name }, false);
	if (report === REFUSED) return;
	const url = URL.createObjectURL(
		new Blob([JSON.stringify(report, null, 2)], { type: "application/json" })
	);
	const link = document.createElement("a");
	link.href = url;
	link.download = "ucm-migration-" + frm.doc.name + ".json";
	link.click();
	setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Whether this run's form is the page in front of the user: a timer left by another form does nothing.
function showing(frm) {
	const route = frappe.get_route();
	return route[0] === "Form" && route[1] === frm.doctype && route[2] === frm.doc.name;
}

// Approve a hand-off its publishing account proposed. The approver types the check code the publishing operator
// gave them: it is the short form of the file's fingerprint, which this dialog does not show.
async function approve_proposed(frm) {
	const found = await ask(METHOD + "inspect_proposed", { run: frm.doc.name }, false);
	if (found === REFUSED) return;
	const dialog = new frappe.ui.Dialog({
		title: __("Approve the hand-off of {0}", [found.display_id]),
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options: handoff_summary(found, [__("Proposed by"), safe(found.proposed_by)]),
			},
			{
				fieldname: "check_code",
				fieldtype: "Data",
				label: __("Check code"),
				reqd: 1,
				description: __(
					"The code the publishing operator's command gave them, as they told it to you. It ties your approval to their sealed file."
				),
			},
			...context_fields(found),
		],
		primary_action_label: __("Approve"),
		primary_action: async (values) => {
			const approved = await ask(METHOD + "approve_proposed", {
				run: frm.doc.name,
				check_code: values.check_code,
				context_code: found.context.exists ? null : values.context_code,
				context_name: found.context.exists ? null : values.context_name,
			});
			// refused: the dialog stays for another try
			if (approved === REFUSED) return;
			dialog.hide();
			frappe.show_alert({
				message: __("Approved. It is staged when its file has arrived."),
				indicator: "green",
			});
			await frm.reload_doc();
		},
	});
	if (!approvable(found)) dialog.get_primary_btn().prop("disabled", true);
	dialog.show();
}

// Ask for the staging of the arrived file. The worker stages it: say so, and show the outcome when it is there.
async function stage_now(frm) {
	const before = frm.doc.modified;
	const result = await ask(METHOD + "stage_now", { run: frm.doc.name });
	if (result === REFUSED) return;
	const said = {
		queued: [
			__("Staging requested. This page shows the result when the worker has finished."),
			"blue",
		],
		staged: [__("Staged: the file is checked, nothing is written yet."), "green"],
		refused: [__("Refused: the run says what differs."), "red"],
		incomplete: [__("The file has not arrived completely yet."), "orange"],
		"not approved": [__("This run has no approval on this site."), "orange"],
	}[result.outcome];
	if (said) frappe.show_alert({ message: said[0], indicator: said[1] });
	await frm.reload_doc();
	if (result.outcome !== "queued") return;
	// staged, refused again or failed, the run is written: a large file takes the worker a while to check
	const until = Date.now() + 4 * 60 * 60 * 1000;
	clearInterval(frm.ucm_staging);
	frm.ucm_staging = setInterval(async () => {
		if (!showing(frm)) return clearInterval(frm.ucm_staging);
		if (cur_dialog) return;
		await frm.reload_doc();
		if (frm.doc.modified !== before || Date.now() > until) clearInterval(frm.ucm_staging);
	}, 5000);
}

// The worker opens the delegation after the request: show the result without a click, while this form stays open.
function follow_activation(frm) {
	frappe.show_alert({
		message: __(
			"Activation requested. This page shows the result when the worker has finished."
		),
		indicator: "blue",
	});
	const until = Date.now() + 30 * 60 * 1000;
	clearInterval(frm.ucm_activation);
	frm.ucm_activation = setInterval(async () => {
		if (!showing(frm)) return clearInterval(frm.ucm_activation);
		if (cur_dialog) return;
		await frm.reload_doc();
		// done, or refused with a finding; a worker that never answers is not waited for all day
		if (frm.doc.state !== STATE.READY || frm.doc.finding || Date.now() > until)
			clearInterval(frm.ucm_activation);
	}, 3000);
}

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
	const counts = [applied, total, remaining].map((n) => n.toLocaleString());
	let message = __("{0} of {1} batches finished · {2} remaining", counts);
	if (frm.doc.state === STATE.READY && remaining > 0) {
		message += " · " + __("Remaining batches run at activation.");
	} else if ([STATE.CLEANING, STATE.DISCARDED].includes(frm.doc.state)) {
		message = __("{0} of {1} batches currently applied", counts);
	}
	if (frm.doc.worker_phase && frm.doc.worker_seen) {
		// a phase that names a DocType ("Writing Person") is stored whole and shown as stored
		message +=
			" · " +
			__("{0} · {1} batches reconciled · Last checkpoint {2}", [
				__(frm.doc.worker_phase),
				Number(frm.doc.reconcile_cursor || 0),
				frappe.datetime.str_to_user(frm.doc.worker_seen),
			]);
	}
	frm.dashboard.show_progress(
		__("Batch progress"),
		Math.floor((100 * applied) / total),
		message
	);
}

// The route the server keeps for the run: one route for the delegation, or routes chosen per DocType. A run that
// has no file yet has none.
function show_write_route(frm) {
	const route = frm.doc.write_route;
	if (!route) return;
	const label = route === BY_DOCTYPE ? __("chosen per DocType") : __(route);
	frm.dashboard.set_headline(__("Write route: {0}", [label]));
}

async function configure_write_routes(frm) {
	const run = frm.doc.name;
	const rows = await ask(METHOD + "route_options", { run }, false);
	if (rows === REFUSED) return;
	const whole = rows.length && rows.every((row) => row.selected === SET_BASED);
	const dialog = new frappe.ui.Dialog({
		title: __("Configure write routes"),
		fields: [
			{
				fieldname: "whole",
				label: __("Route for the delegation"),
				fieldtype: "Select",
				options: [
					{ value: BY_DOCTYPE, label: __("Chosen per DocType") },
					{ value: SET_BASED, label: __("Set-based SQL") },
				],
				default: whole ? SET_BASED : BY_DOCTYPE,
				description: __(
					"Set-based SQL writes every DocType in sets, then derives and verifies the whole delegation before it can be activated."
				),
			},
			...rows.map((row, i) => ({
				fieldname: "route_" + i,
				label: row.doctype,
				fieldtype: "Select",
				options: row.sql_allowed
					? ["Document API", "Validated bulk SQL"]
					: ["Document API"],
				default: row.sql_allowed && !whole ? row.selected : "Document API",
				read_only: !row.sql_allowed,
				depends_on: `eval:doc.whole !== "${SET_BASED}"`,
				description: row.sql_allowed
					? undefined
					: __("Bulk SQL is unavailable for this DocType on this site."),
			})),
		],
		primary_action_label: __("Save routes"),
		async primary_action(values) {
			const chosen =
				values.whole === SET_BASED
					? { route: SET_BASED }
					: {
							routes: Object.fromEntries(
								rows.map((row, i) => [
									row.doctype,
									row.sql_allowed ? values["route_" + i] : "Document API",
								])
							),
					  };
			// refused: the dialog stays, and the server's message says why
			if ((await ask(METHOD + "choose_routes", { run, ...chosen })) === REFUSED) return;
			dialog.hide();
			await frm.reload_doc();
		},
	});
	dialog.show();
}

// ---------------------------------------------------------------------------------------------------------------
// The overview of a sealed publication's run: its steps, the rows on both sides and the verification's findings.
// The server sends numbers and fixed names (run_overview.build); the sentences are made here.

const STATUS = {
	done: [__("Done"), "green"],
	running: [__("Running"), "blue"],
	"no job": [__("No job"), "red"],
	paused: [__("Paused"), "orange"],
	waiting: [__("Waiting"), "gray"],
	failed: [__("Failed"), "red"],
	decision: [__("Decision"), "yellow"],
	stopped: [__("Stopped"), "gray"],
};
const SEVERITY = {
	CRITICAL: [__("Critical"), "red"],
	HIGH: [__("High"), "orange"],
	MEDIUM: [__("Medium"), "yellow"],
	LOW: [__("Low"), "gray"],
};
const STEP_LABEL = {
	stage: __("Check and stage"),
	write: __("Write"),
	derive: __("Derive"),
	verify: __("Verify"),
	"hand over": __("Hand over to data quality"),
	activate: __("Activate"),
	discard: __("Discard"),
};
// what a verification can find and what a derivation changes: fixed names from the server, shown translated
const CHECK = {
	"rows missing in UCM": __("Rows missing in UCM"),
	"rows in UCM that were not published": __("Rows in UCM that were not published"),
	"published rows not found by their key": __("Published rows not found by their key"),
	"field differs from the publication": __("Field differs from the publication"),
	"rule broken": __("Rule broken"),
	"batch receipts differ from the publication": __("Batch receipts differ from the publication"),
};
const DERIVED = {
	"place geography": __("Place geography"),
	"department copies": __("Department copies"),
	"country copies": __("Country copies"),
	"share facts": __("Share facts"),
	"latest state marks": __("Latest state marks"),
};
const pill = ([label, color]) =>
	`<span class="indicator-pill ${color} no-indicator-dot">${safe(label)}</span>`;

function spell_seconds(seconds) {
	if (seconds === null || seconds === undefined) return "";
	const whole = Math.round(seconds);
	if (whole < 60) return __("{0} s", [seconds < 10 ? Math.round(seconds * 10) / 10 : whole]);
	if (whole < 3600) return __("{0} min {1} s", [Math.floor(whole / 60), whole % 60]);
	return __("{0} h {1} min", [Math.floor(whole / 3600), Math.floor((whole % 3600) / 60)]);
}

function refusal(facts) {
	const parts = [];
	const tables = facts.schema_differences || [];
	if (tables.length)
		parts.push(__("Tables that differ from this site: {0}", [tables.join(", ")]));
	for (const [app, versions] of Object.entries(facts.release_differences || {})) {
		parts.push(
			__("{0}: published with {1}, this site has {2}", [
				app,
				versions.published,
				versions.target || __("none"),
			])
		);
	}
	const keys = Object.entries(facts.existing_keys || {}).filter(([, n]) => n);
	if (keys.length)
		parts.push(
			__("Record IDs already on this site: {0}", [
				keys.map(([dt, n]) => `${dt} ${number(n)}`).join(", "),
			])
		);
	if (facts.existing_share_receipts)
		parts.push(
			__("{0} share receipts already kept for these IDs", [
				number(facts.existing_share_receipts),
			])
		);
	if (facts.removed_on)
		parts.push(
			__("The file that arrived was removed from the site by {0} on {1}", [
				facts.removed_by,
				frappe.datetime.str_to_user(facts.removed_on),
			])
		);
	return parts.join(" · ");
}

function verify_detail(step, set_based) {
	const facts = step.facts;
	if (step.status === "failed") {
		return facts.findings === 1
			? __("1 finding, listed below")
			: __("{0} findings, listed below", [number(facts.findings)]);
	}
	if (step.status === "done") {
		return set_based
			? __(
					"{0} rows compared with the publication, {1} rules checked, {2} records through the documents: nothing found",
					[number(facts.rows), number(facts.rules), number(facts.sampled)]
			  )
			: __("{0} rows read back through the documents and compared: nothing found", [
					number(facts.rows),
			  ]);
	}
	if (["running", "paused"].includes(step.status) && facts.business) {
		return __("{0} of {1} batches compared", [
			number(facts.reconciled),
			number(facts.business),
		]);
	}
	return "";
}

function step_detail(step, data) {
	const facts = step.facts || {};
	const set_based = data.facts.route === SET_BASED;
	if (step.step === "stage") {
		if (step.status === "failed") return refusal(facts);
		if (data.state === STATE.PROPOSED) {
			return __(
				"Proposed by {0} on {1}: a sealed file of {2}. Waiting for a Migration Admin's approval.",
				[
					facts.proposed_by,
					frappe.datetime.str_to_user(facts.proposed_on),
					megabytes(facts.bytes),
				]
			);
		}
		if (data.state === STATE.AWAITING) {
			return __("Approved by {0} on {1}, to be sent by {2}. {3} of {4} arrived.", [
				facts.by,
				frappe.datetime.str_to_user(facts.on),
				facts.publisher,
				megabytes(facts.arrived),
				megabytes(facts.bytes),
			]);
		}
		return __("{0} batches, {1} rows, {2} tables with the columns of this site", [
			number(facts.batches),
			number(facts.rows),
			number(facts.tables),
		]);
	}
	if (step.step === "write") {
		if (!facts.batches) return "";
		// a run loaded before the receipts kept the rows of a batch
		if (!facts.parents) return __("{0} batches", [number(facts.batches)]);
		const routes = Object.entries(facts.by_route || {}).map(([route, n]) =>
			__("{0} by {1}", [number(n), __(route)])
		);
		return (
			__("{0} records in {1} batches", [number(facts.parents), number(facts.batches)]) +
			(routes.length > 1 ? " · " + routes.join(", ") : "")
		);
	}
	if (step.step === "derive") {
		if (step.status !== "done") return "";
		const changed = Object.entries(facts.changed || {}).filter(([, n]) => n);
		if (!changed.length)
			return __("Nothing to change: the publication carried what UCM derives");
		return changed
			.map(([what, n]) => __("{0}: {1} rows", [DERIVED[what] || what, number(n)]))
			.join(" · ");
	}
	if (step.step === "verify") return verify_detail(step, set_based);
	if (step.step === "hand over") {
		if (step.status === "done") {
			return __("{0} metrics kept in {1}, recorded by {2} on {3}", [
				number(facts.rows),
				facts.destination,
				facts.by,
				frappe.datetime.str_to_user(facts.on),
			]);
		}
		if (facts.on) {
			return __(
				"Handed over on {0}, before the run's last step. Run ucm-push quality again on the migration side.",
				[frappe.datetime.str_to_user(facts.on)]
			);
		}
		return __("Not handed over yet. Run ucm-push quality on the migration side.");
	}
	if (step.step === "activate") {
		if (step.status === "decision") {
			const held = facts.deferred || {};
			return __(
				"Needs a decision. {0} assignments and {1} shares are released at activation.",
				[number(held.ToDo), number(held["Record Share"])]
			);
		}
		if (step.status !== "done") return "";
		const released = Object.values(facts.released || {}).reduce((sum, n) => sum + n, 0);
		return __(
			"By {0} on {1}. {2} assignments and shares released, {3} shares held for delegations not yet live.",
			[facts.by, frappe.datetime.str_to_user(facts.on), number(released), number(facts.held)]
		);
	}
	return "";
}

function steps_html(data) {
	const rows = data.steps.map(
		(step) => `
		<tr>
			<td style="width: 6.5rem">${pill(STATUS[step.status] || [step.status, "gray"])}</td>
			<td>
				<div><b>${safe(STEP_LABEL[step.step] || step.step)}</b>
					<span class="text-muted">${safe(spell_seconds(step.seconds))}</span></div>
				<div class="text-muted small">${safe(step_detail(step, data))}</div>
			</td>
		</tr>`
	);
	return `<h6 class="uppercase">${__(
		"Steps"
	)}</h6><table class="table table-borderless table-sm">${rows.join("")}</table>`;
}

function facts_html(data) {
	const facts = data.facts;
	const when = (moment) => moment && frappe.datetime.str_to_user(moment);
	const versions = Object.entries(facts.versions)
		.map(([app, version]) => `${app} ${version}`)
		.join(", ");
	const rows = [
		[__("Delegation"), data.delegation],
		[__("Route"), __(facts.route)],
		[__("Snapshot"), facts.snapshot],
		[__("Proposed"), when(facts.proposed_on)],
		[__("Proposed by"), facts.proposed_by],
		[__("Approved"), when(facts.approved_on)],
		[__("Approved by"), facts.approved_by],
		[__("Staged"), when(facts.staged_on)],
		[__("Staged by"), facts.staged_by],
		[__("Publishing account"), facts.publishing_user],
		[__("Published with"), versions],
	].filter(([, value]) => value);
	return `<h6 class="uppercase">${__(
		"Run"
	)}</h6><table class="table table-borderless table-sm">${rows
		.map(
			([label, value]) =>
				`<tr><td class="text-muted" style="width: 9rem">${safe(
					label
				)}</td><td style="overflow-wrap: anywhere">${safe(value)}</td></tr>`
		)
		.join("")}</table>`;
}

function records_html(data) {
	let published = 0,
		stored = 0,
		counted = false;
	const rows = data.records.map((row) => {
		published += row.published;
		let target = "",
			difference = "";
		if (row.stored === null) {
			if (row.deferred && data.state !== STATE.REJECTED)
				target = `<span class="text-muted">${__("held until activation")}</span>`;
		} else {
			counted = true;
			stored += row.stored + row.held;
			target =
				number(row.stored) +
				(row.held
					? ` <span class="text-muted">${__("+ {0} held", [number(row.held)])}</span>`
					: "");
			const missing = row.published - row.stored - row.held;
			difference = missing ? `<span class="text-danger">${number(-missing)}</span>` : "0";
		}
		return `<tr><td>${safe(__(row.doctype))}</td><td class="text-right">${number(
			row.published
		)}</td>
			<td class="text-right">${target}</td><td class="text-right">${difference}</td></tr>`;
	});
	return `<h6 class="uppercase">${__("Records")}</h6>
		<table class="table table-bordered table-sm">
			<thead><tr><th>${__("Table")}</th><th class="text-right">${__("Published")}</th>
				<th class="text-right">${__("In UCM")}</th><th class="text-right">${__(
		"Difference"
	)}</th></tr></thead>
			<tbody>${rows.join("")}</tbody>
			<tfoot><tr><th>${__("Total")}</th><th class="text-right">${number(published)}</th>
				<th class="text-right">${counted ? number(stored) : ""}</th><th></th></tr></tfoot>
		</table>`;
}

function findings_html(data) {
	if (!data.findings.length) return "";
	const rows = data.findings.map(
		(row) => `
		<tr>
			<td>${safe(CHECK[row.check] || row.check)}${
			row.detail ? `<div class="text-muted small">${safe(row.detail)}</div>` : ""
		}</td>
			<td>${safe(__(row.doctype))}</td>
			<td class="text-right">${number(row.found)}</td>
			<td>${pill(SEVERITY[row.severity] || [row.severity, "gray"])}</td>
			<td>${row.batch ? safe(__("Batch {0}, item {1}", [row.batch, row.item])) : ""}</td>
		</tr>`
	);
	return `<h6 class="uppercase">${__("Findings")}</h6>
		<table class="table table-bordered table-sm">
			<thead><tr><th>${__("Check")}</th><th>${__("Table")}</th><th class="text-right">${__("Rows")}</th>
				<th>${__("Severity")}</th><th>${__("First place in the publication")}</th></tr></thead>
			<tbody>${rows.join("")}</tbody>
		</table>
		<p class="text-muted small">${__(
			"The run stays paused whatever the severity. Places are a batch and an item of the sealed file, never a record's content."
		)}</p>`;
}

function show_intro(frm, data) {
	const state = data.state,
		who = data.delegation;
	if (data.stalled) {
		// the job's process was killed or its worker went down: the run keeps its state, and nothing works on it
		const text =
			state === STATE.CLEANING
				? __(
						"No job is working on this discard: its worker stopped. Choose Migration, Resume cleanup."
				  )
				: __(
						"No job is working on this load: its worker stopped after batch {0}. Nothing is half written. Choose Migration, Resume from last receipt.",
						[number(data.batches_committed)]
				  );
		frm.set_intro(text, "red");
		return;
	}
	if (state === STATE.PROPOSED) {
		frm.set_intro(
			__(
				"The publishing account proposes this publication of {0}. Nothing is accepted for it until a Migration Admin approves the hand-off, with the check code the publishing operator gives them.",
				[who]
			),
			"yellow"
		);
		return;
	}
	if (state === STATE.AWAITING) {
		if (data.finding) frm.set_intro(data.finding, "orange");
		else {
			frm.set_intro(
				__(
					"The hand-off of {0} is approved. The run waits for the sealed file, and is staged by itself when the whole file has arrived.",
					[who]
				),
				"blue"
			);
		}
		return;
	}
	if (state === STATE.READY) {
		frm.set_intro(
			__(
				"All checks passed. {0} is hidden from users until it is activated. Activation is recorded with your name and the time.",
				[who]
			),
			"blue"
		);
	} else if (state === STATE.PAUSED && data.findings.length) {
		const text =
			data.findings.length === 1
				? __(
						"1 finding. {0} cannot be activated and stays hidden. Correct the publication or the reference data, then resume or discard the run.",
						[who]
				  )
				: __(
						"{0} findings. {1} cannot be activated and stays hidden. Correct the publication or the reference data, then resume or discard the run.",
						[data.findings.length, who]
				  );
		frm.set_intro(text, "red");
	} else if ([STATE.PAUSED, STATE.REJECTED].includes(state) && data.finding) {
		frm.set_intro(data.finding, state === STATE.REJECTED ? "red" : "orange");
	} else if (state === STATE.ACTIVATED) {
		frm.set_intro(__("{0} is open to its users.", [who]), "green");
	} else if (state === STATE.DISCARDED) {
		frm.set_intro(
			__("This run was discarded. Its rows are removed and its receipts are kept.")
		);
	}
}

async function show_overview(frm) {
	const field = frm.get_field("overview");
	if (!field) return;
	const run = frm.doc.name;
	const data = await ask(METHOD + "overview", { run }, false);
	// one form serves every run: an answer that comes after the user opened another run is not shown on it
	if (frm.doc.name !== run) return;
	if (data === REFUSED || !data || !data.publication) {
		field.$wrapper.empty();
		return;
	}
	show_intro(frm, data);
	field.$wrapper.html(`
		<div class="row">
			<div class="col-md-7">${steps_html(data)}</div>
			<div class="col-md-5">${facts_html(data)}</div>
		</div>
		${findings_html(data)}
		${records_html(data)}`);
	if (data.quality_url) {
		frm.add_custom_button(__("Open in data quality dashboard"), () =>
			window.open(data.quality_url, "_blank", "noopener")
		);
	}
	// a run at work, or waiting for its file: show its progress without a click, while this form stays open.
	// A run whose job stopped is not at work: its notice names the action that resumes it.
	clearTimeout(frm.ucm_refresh);
	const at_work = [STATE.APPLYING, STATE.CLEANING, STATE.AWAITING].includes(data.state);
	if (at_work && !data.stalled) {
		frm.ucm_refresh = setTimeout(() => {
			if (showing(frm) && !cur_dialog) frm.reload_doc();
		}, 10000);
	}
}
