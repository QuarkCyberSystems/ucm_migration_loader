"""The site operator's commands, on the bench of the receiving site.

	bench --site <site> ucm-migration approve <handoff.json> --publisher <account>
	bench --site <site> ucm-migration stage <handoff.json>

The Desk does both: a Migration Admin approves a hand-off from the list of runs, and the file is staged by itself
when it has arrived. These commands are the same steps from the bench, for a site without a browser at hand and
for scripts. Each says in a sentence what happened and what comes next.
"""

import sys

import click
import frappe
from frappe.commands import get_site, pass_context


def megabytes(size: int) -> str:
	return f"{size / 1_000_000:,.1f} MB"


def said(result: dict, handoff: str) -> tuple[str, int]:
	"""What the operator reads for an outcome, and the command's exit code."""
	short = result["run"][:12] + "…"
	if result.get("publisher"):
		return (
			f"Approved run {short} for {result['publisher']}: {result['rows']:,} rows in "
			f"{result['batches']:,} batches, {megabytes(result['bytes'])}.\n"
			f"It waits for its file as {result['display_id']}. The publishing operator can send the file now: "
			"it is staged by itself when all of it has arrived.\n"
			f"To stage it by hand: bench --site {frappe.local.site} ucm-migration stage {handoff}"
		), 0
	outcome = result["outcome"]
	if outcome == "staged":
		return (
			f"Staged as {result['display_id']}: {result['rows']:,} rows in {result['batches']:,} batches, "
			"nothing written yet.\n"
			f"Open UCM Migration Run {result['display_id']} in the Desk to choose the route and load."
		), 0
	if outcome == "refused":
		return (
			f"Refused, and kept as {result['display_id']}. {result['finding']}.\n"
			"Open the run in the Desk to read what differs. Nothing of the file was kept."
		), 1
	if outcome == "incomplete":
		return (
			f"The file of run {short} has not arrived completely: {megabytes(result['arrived'])} of "
			f"{megabytes(result['bytes'])}.\nAsk the publishing operator to run ucm-push send again."
		), 1
	return (
		f"Run {short} was not approved on this site. Approve its hand-off first:\n"
		f"  bench --site {frappe.local.site} ucm-migration approve {handoff} --publisher <account>"
	), 1


def on_site(context, work) -> None:
	"""Do one piece of work on the site, keep it, and say how it went."""
	frappe.init(get_site(context))
	frappe.connect()
	try:
		result, handoff = work()
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a bench command, as bench execute commits
		frappe.db.commit()
		text, code = said(result, handoff)
	except (ValueError, frappe.ValidationError) as refused:
		frappe.db.rollback()
		text, code = f"Not done: {refused}", 1
	finally:
		frappe.destroy()
	click.echo(text)
	sys.exit(code)


@click.group("ucm-migration")
def ucm_migration():
	"""Receive a sealed delegation on this site."""


@ucm_migration.command("approve")
@click.argument("handoff", type=click.Path(exists=True, dir_okay=False))
@click.option("--publisher", required=True, help="The publishing account that will send the file")
@pass_context
def approve(context, handoff, publisher):
	"""Approve a reviewed hand-off for the account that will send its file."""
	from ucm_migration_loader.operator import approve_handoff

	on_site(context, lambda: (approve_handoff(handoff, publisher), handoff))


@ucm_migration.command("stage")
@click.argument("handoff")
@pass_context
def stage(context, handoff):
	"""Check an approved file that has arrived, and stage it as a run. HANDOFF: the hand-off file, or the run."""
	from ucm_migration_loader.operator import stage_arrived

	on_site(context, lambda: (stage_arrived(handoff), handoff))


commands = [ucm_migration]
