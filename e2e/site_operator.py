"""A Migration Admin in Desk, in a browser: approve the hand-off of a sealed publication, and see the run that
waits for the file.

	env/bin/python apps/ucm_migration_loader/e2e/site_operator.py --site <site> \
		--proposed <run> --code <check code> [--shots]
	env/bin/python apps/ucm_migration_loader/e2e/site_operator.py --site <site> \
		--handoff <handoff.json> --publisher <account> [--shots]

Run from the bench, on a development site that is being served. The first form approves a hand-off its publishing
account proposed with `ucm-push propose`, by the check code that command gave; a wrong code is tried first and
must be refused. The second approves a hand-off from its file, for an account named in the dialog. The approval
is real: the run is left waiting for its file, and is staged by the site's worker when the file has arrived
(`tests/operator_flow.py --desk` sends it). With `--shots`
the dialog and the waiting run are saved as pictures of the user manual. A session is opened for the
Administrator, as `bench browse` does, and closed at the end. Needs Playwright and Chromium. Synthetic data only.
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


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--site", required=True)
	parser.add_argument("--proposed", metavar="RUN", help="Approve this proposed hand-off")
	parser.add_argument("--code", help="The check code the publishing operator gave, with --proposed")
	parser.add_argument("--handoff", type=pathlib.Path, help="Approve the hand-off in this file")
	parser.add_argument("--publisher", help="The publishing account, with --handoff")
	parser.add_argument(
		"--shots", action="store_true", help="Save the dialog and the waiting run as pictures"
	)
	args = parser.parse_args()
	if bool(args.proposed and args.code) == bool(args.handoff and args.publisher):
		parser.error("Give --proposed with --code, or --handoff with --publisher")
	handoff = args.handoff.resolve() if args.handoff else None
	errors = []
	with desk(args.site) as page:
		page.on("pageerror", lambda error: errors.append(str(error)))
		if args.proposed:
			run = approve_proposed(page, args.proposed, args.code, args.shots)
		else:
			run = approve(page, handoff, args.publisher, args.shots)
	if errors:
		raise SystemExit(f"The page raised errors: {errors[:3]}")
	print(f"Approved in the Desk: run {run} waits for its file")


if __name__ == "__main__":
	main()
