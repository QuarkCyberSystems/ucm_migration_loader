"""A Migration Admin in Desk, in a browser: approve the hand-off of a sealed publication, and see the run that
waits for the file.

	env/bin/python apps/ucm_migration_loader/e2e/site_operator.py --site <site> \
		--proposed <run> --code <check code> [--shots]
	env/bin/python apps/ucm_migration_loader/e2e/site_operator.py --site <site> \
		--handoff <handoff.json> --publisher <account> [--shots]
	env/bin/python apps/ucm_migration_loader/e2e/site_operator.py --site <site> --reader <run> --as <account>
	env/bin/python apps/ucm_migration_loader/e2e/site_operator.py --site <site> --refused <run>

Run from the bench, on a development site that is being served. The first form approves a hand-off its publishing
account proposed with `ucm-push propose`, by the check code that command gave; a wrong code is tried first and
must be refused. The second approves a hand-off from its file, for an account named in the dialog. The approval
is real: the run is left waiting for its file, and is staged by the site's worker when the file has arrived
(`tests/operator_flow.py --desk` sends it). With `--shots`
the dialog and the waiting run are saved as pictures of the user manual. The third form opens a run as an account
that reads runs without the Migration Admin role, a System Manager for instance, and checks that the form offers
it no action. The fourth opens a run the site refused, removes the file that arrived for it, and checks that
the run stays refused, says who removed the file, and offers neither the staging nor the removal again. A session is opened for the Administrator, or for the account named, as `bench browse` does, and
closed at the end. Needs Playwright and Chromium. Synthetic data only.
"""

import argparse
import json
import pathlib

from manual_screenshots import OUT, PLAIN, cut, desk
from playwright.sync_api import expect


def approve(page, handoff: pathlib.Path, publisher: str, shots: bool) -> str:
	"""Approve a hand-off from the list of runs. Returns the run."""
	about = json.loads(handoff.read_text())
	page.goto("/desk/ucm-migration-run", wait_until="networkidle")
	page.locator(".page-actions button", has_text="Approve a hand-off").first.click()
	dialog = page.locator(".modal-dialog:visible")
	approve_button = dialog.get_by_role("button", name="Approve", exact=True)
	expect(approve_button).to_be_disabled()
	dialog.locator(".ucm-handoff-file").set_input_files(str(handoff))
	summary = dialog.locator('[data-fieldname="summary"]')
	expect(summary.get_by_text(about["snapshot"], exact=True)).to_be_visible()
	expect(summary.get_by_text(about["file_sha256"], exact=True)).to_be_visible()
	expect(approve_button).to_be_enabled()
	# the account is chosen as the dialog's own field takes it: a typed name opens a list of suggestions over
	# the rest of the dialog
	page.evaluate("(account) => cur_dialog.set_value('publishing_user', account)", publisher)
	expect(dialog.locator('[data-fieldname="publishing_user"] input')).to_have_value(publisher)
	page.wait_for_timeout(500)
	if shots:
		OUT.mkdir(parents=True, exist_ok=True)
		dialog.locator(".modal-content").screenshot(path=OUT / "approve-handoff.png")
	approve_button.click()
	page.wait_for_url(f"**/ucm-migration-run/{about['run']}", timeout=30000)
	page.add_style_tag(content=PLAIN)
	overview = page.locator('.frappe-control[data-fieldname="overview"]').locator("visible=true")
	expect(overview.get_by_text("Steps", exact=True)).to_be_visible()
	expect(page.locator(".form-message").get_by_text("waits for the sealed file")).to_be_visible()
	expect(overview.get_by_text("to be sent by " + publisher)).to_be_visible()
	if shots:
		page.wait_for_timeout(500)
		cut(page, "run-waiting", overview.locator(".row").first, pad=-8)
	return about["run"]


def approve_proposed(page, run: str, code: str, shots: bool) -> str:
	"""Approve a proposed hand-off from its run, with the check code the publishing operator gave."""
	page.goto(f"/desk/ucm-migration-run/{run}", wait_until="networkidle")
	page.add_style_tag(content=PLAIN)
	overview = page.locator('.frappe-control[data-fieldname="overview"]').locator("visible=true")
	expect(overview.get_by_text("Steps", exact=True)).to_be_visible()
	expect(page.locator(".form-message").get_by_text("proposes this publication")).to_be_visible()
	expect(overview.get_by_text("Waiting for a Migration Admin's approval")).to_be_visible()
	if shots:
		page.wait_for_timeout(500)
		cut(page, "run-proposed", overview.locator(".row").first, pad=-8)
	page.locator(".page-actions button.btn-primary", has_text="Approve hand-off").first.click()
	dialog = page.locator(".modal-dialog:visible")
	expect(dialog.get_by_text("Check code", exact=True)).to_be_visible()
	# the dialog shows what is proposed, and neither the file's fingerprint nor the code
	expect(dialog.get_by_text(code, exact=False)).to_have_count(0)
	field = dialog.locator('[data-fieldname="check_code"] input')
	field.fill("0000-0000")
	dialog.get_by_role("button", name="Approve", exact=True).click()
	refused = page.locator(".modal-dialog:visible", has_text="check code does not match")
	expect(refused).to_be_visible()
	refused.locator(".btn-modal-close").first.click()
	expect(refused).to_be_hidden()
	field.fill(code)
	if shots:
		page.wait_for_timeout(400)
		OUT.mkdir(parents=True, exist_ok=True)
		dialog.locator(".modal-content").screenshot(path=OUT / "approve-proposed.png")
	dialog.get_by_role("button", name="Approve", exact=True).click()
	expect(page.locator(".form-message").get_by_text("waits for the sealed file")).to_be_visible(
		timeout=30000
	)
	page.add_style_tag(content=PLAIN)
	if shots:
		page.wait_for_timeout(500)
		cut(page, "run-waiting", overview.locator(".row").first, pad=-8)
	return run


def reads_only(page, run: str) -> str:
	"""Who reads a run without the Migration Admin role sees it whole, and is offered no action on it."""
	page.goto(f"/desk/ucm-migration-run/{run}", wait_until="networkidle")
	overview = page.locator('.frappe-control[data-fieldname="overview"]').locator("visible=true")
	expect(overview.get_by_text("Steps", exact=True)).to_be_visible()
	actions = page.locator(".page-actions")
	expect(actions.get_by_text("View batches", exact=True).first).to_be_visible()
	expect(page.locator(".page-actions button.btn-primary:visible")).to_have_count(0)
	menu = page.locator(".inner-group-button", has_text="Migration").locator("visible=true").first
	menu.click()
	offered = sorted(text.strip() for text in menu.locator(".dropdown-item").all_inner_texts())
	if offered != ["Refresh progress"]:
		raise SystemExit(f"A reader is offered: {offered}")
	page.goto("/desk/ucm-migration-run", wait_until="networkidle")
	expect(page.locator(".page-actions button", has_text="Approve a hand-off")).to_have_count(0)
	return run


def remove_refused(page, run: str) -> str:
	"""A Migration Admin removes the file that arrived for a refused run: the run stays, as the record."""
	page.goto(f"/desk/ucm-migration-run/{run}", wait_until="networkidle")
	overview = page.locator('.frappe-control[data-fieldname="overview"]').locator("visible=true")
	expect(overview.get_by_text("Steps", exact=True)).to_be_visible()
	menu = page.locator(".inner-group-button", has_text="Migration").locator("visible=true").first
	offered = {text.strip() for text in menu.locator(".dropdown-item").all_inner_texts()}
	if not {"Stage now", "Remove arrived file"} <= offered:
		raise SystemExit(f"A refused run offers: {sorted(offered)}")
	menu.locator("button").first.click()
	# the opened menu is drawn apart from the group's own list of items
	page.locator(".es-menu__item:visible", has_text="Remove arrived file").click()
	question = page.locator(".modal-dialog:visible", has_text="cannot be staged afterwards")
	expect(question).to_be_visible()
	question.get_by_role("button", name="Yes", exact=True).click()
	expect(overview.get_by_text("The file that arrived was removed from the site by")).to_be_visible(
		timeout=30000
	)
	menu = page.locator(".inner-group-button", has_text="Migration").locator("visible=true").first
	offered = {text.strip() for text in menu.locator(".dropdown-item").all_inner_texts()}
	if offered & {"Stage now", "Remove arrived file"}:
		raise SystemExit(f"A refused run whose file is removed still offers: {sorted(offered)}")
	return run


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--site", required=True)
	parser.add_argument("--proposed", metavar="RUN", help="Approve this proposed hand-off")
	parser.add_argument("--code", help="The check code the publishing operator gave, with --proposed")
	parser.add_argument("--handoff", type=pathlib.Path, help="Approve the hand-off in this file")
	parser.add_argument("--publisher", help="The publishing account, with --handoff")
	parser.add_argument("--reader", metavar="RUN", help="Check that this run offers a reader no action")
	parser.add_argument("--as", dest="account", help="The account that reads, with --reader")
	parser.add_argument("--refused", metavar="RUN", help="Remove the file that arrived for this refused run")
	parser.add_argument(
		"--shots", action="store_true", help="Save the dialog and the waiting run as pictures"
	)
	args = parser.parse_args()
	ways = (
		args.proposed and args.code,
		args.handoff and args.publisher,
		args.reader and args.account,
		args.refused,
	)
	if sum(bool(way) for way in ways) != 1:
		parser.error(
			"Give --proposed with --code, --handoff with --publisher, --reader with --as, or --refused"
		)
	handoff = args.handoff.resolve() if args.handoff else None
	errors = []
	with desk(args.site, args.account or "Administrator") as page:
		page.on("pageerror", lambda error: errors.append(str(error)))
		if args.reader:
			run = reads_only(page, args.reader)
		elif args.refused:
			run = remove_refused(page, args.refused)
		elif args.proposed:
			run = approve_proposed(page, args.proposed, args.code, args.shots)
		else:
			run = approve(page, handoff, args.publisher, args.shots)
	if errors:
		raise SystemExit(f"The page raised errors: {errors[:3]}")
	if args.reader:
		print(f"Read in the Desk as {args.account}: run {run} offers no action")
	elif args.refused:
		print(f"Removed in the Desk: the file that arrived for the refused run {run}")
	else:
		print(f"Approved in the Desk: run {run} waits for its file")


if __name__ == "__main__":
	main()
