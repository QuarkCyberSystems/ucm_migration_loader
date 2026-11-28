// What the form and the list of UCM Migration Run share: the names the server uses, and the summary of a hand-off
// for the person who approves it. hooks.py adds this file to both scripts, after their own code: what is declared
// here is used inside their functions, never at their top level.

const ROLE = "Migration Admin";
const STATE = {
	PROPOSED: "Proposed",
	AWAITING: "Awaiting publication",
	STAGED: "Publication staged",
	APPLYING: "Publication applying",
	PAUSED: "Publication paused",
	READY: "Publication ready",
	CLEANING: "Publication cleaning",
	ACTIVATED: "Activated",
	DISCARDED: "Discarded",
	REJECTED: "Rejected",
};
// the worker's phase of a verification that found something, as the server stores it (set_based)
const FOUND_DIFFERENCES = "Verification found differences";
const SET_BASED = "Set-based SQL";
const BY_DOCTYPE = "By DocType";
const METHOD =
	"ucm_migration_loader.ucm_migration_loader.doctype.ucm_migration_run.ucm_migration_run.";

const number = (n) => Number(n || 0).toLocaleString();
const safe = (text) => frappe.utils.escape_html(String(text ?? ""));
const megabytes = (bytes) =>
	(Number(bytes || 0) / 1e6).toLocaleString(undefined, {
		maximumFractionDigits: 1,
		minimumFractionDigits: 1,
	}) + " MB";

// The server's answer to a call, or REFUSED where it refused: its message has told the user why.
const REFUSED = Symbol("refused");
async function ask(method, args, freeze = true) {
	try {
		return (await frappe.call({ method, args, freeze })).message;
	} catch {
		return REFUSED;
	}
}

// The delegation a hand-off is for: as the site's context names it, or as the hand-off does where the site has
// no context yet.
function delegation_of(found) {
	const about = found.delegation || {};
	if (found.context.exists) return `${found.context.name} (${found.context.code})`;
	return about.code ? `${about.name} (${about.code})` : found.context.id;
}

// Whether the hand-off can be approved on this site: the publication is not here already, and its context can
// receive it.
function approvable(found) {
	return !(found.context.problem || (found.known && found.known.state !== STATE.AWAITING));
}

// What stands in the way of the approval, or what the approval will find: a sentence and its colour.
function context_line(found) {
	if (found.known && found.known.state !== STATE.AWAITING) {
		return [
			__("This publication is already on the site as {0}.", [found.known.display_id]),
			"text-danger",
		];
	}
	if (found.context.problem) return [found.context.problem, "text-danger"];
	if (found.context.exists) {
		return [__("Locked, hidden and empty: ready for this publication."), "text-success"];
	}
	return [
		__("This site has no context for it yet. It is made below, locked and hidden."),
		"text-muted",
	];
}

// What a hand-off says, for the person who approves it. `more`: one more row, a label and its HTML, shown
// before the context: the fingerprint of a hand-off read from its file, or who proposed it.
function handoff_summary(found, more) {
	const [context, color] = context_line(found);
	const rows = [
		[__("Delegation"), safe(delegation_of(found))],
		[__("Snapshot"), safe(found.snapshot)],
		[__("Rows"), __("{0} in {1} batches", [number(found.rows), number(found.batches)])],
		[__("Sealed file"), megabytes(found.bytes)],
		more,
		[__("Context"), `<span class="${color}">${safe(context)}</span>`],
	];
	return `<table class="table table-borderless table-sm" style="margin-top: var(--margin-md)">${rows
		.map(
			([label, value]) =>
				`<tr><td class="text-muted" style="width: 8rem">${label}</td><td>${value}</td></tr>`
		)
		.join("")}</table>`;
}

// The code and the name of the context an approval makes, where the site has none. `found`: the hand-off, where
// it is known when the dialog is made.
function context_fields(found) {
	const about = (found && found.delegation) || {};
	return [
		{
			fieldname: "context_section",
			fieldtype: "Section Break",
			label: __("The delegation's context"),
			hidden: found && !found.context.exists ? 0 : 1,
		},
		{ fieldname: "context_code", fieldtype: "Data", label: __("Code"), default: about.code },
		{ fieldname: "context_column", fieldtype: "Column Break" },
		{ fieldname: "context_name", fieldtype: "Data", label: __("Name"), default: about.name },
	];
}
