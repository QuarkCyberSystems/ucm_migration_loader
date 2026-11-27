"""Take the screenshots of the user manual (docs/user-manual.html) from a development site.

	env/bin/python apps/ucm_migration_loader/e2e/manual_screenshots.py --site <site> <shot> [--run <run>]

Each shot is the part of a Desk page that a step of the manual talks about, cut to the page's own width without
the Desk's sidebars, and saved under docs/images/user-guide/. The runs are read as they are; `staged` also opens
the Migration menu and the route dialog, and with `--choose` saves the route chosen there. `tests/` holds the
scripts that bring runs into these situations. Run from the bench, on a site that is being served. A session is
opened for the Administrator, as `bench browse` does, and closed at the end. Needs Playwright and Chromium.
Synthetic data only.
"""

import argparse
import pathlib
from contextlib import contextmanager

from playwright.sync_api import expect, sync_playwright
from set_based_route import close, entry, menu, session

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "images" / "user-guide"
WIDTH, HEIGHT = 1330, 2400
PRIMARY = ".page-actions button.btn-primary"
# the form without its side column, so that a picture is the run and nothing else
PLAIN = """
.layout-side-section { display: none !important; }
.layout-main-section-wrapper { flex: 0 0 100% !important; max-width: 100% !important; width: 100% !important; }
"""


@contextmanager
def desk(site: str):
	"""A page of the site's Desk, signed in."""
	sid, port = session(site)
	try:
		with sync_playwright() as playwright:
			browser = playwright.chromium.launch(args=[f"--host-resolver-rules=MAP {site} 127.0.0.1"])
			context = browser.new_context(
				base_url=f"http://{site}:{port}", viewport={"width": WIDTH, "height": HEIGHT}
			)
			context.add_cookies([{"name": "sid", "value": sid, "domain": site, "path": "/"}])
			page = context.new_page()
			try:
				yield page
			finally:
				browser.close()
	finally:
		close(sid)


def cut(page, name: str, bottom, top: float = 0, pad: int = 14) -> None:
	"""Save the page from `top` down to the end of one element, beside the Desk's sidebar."""
	left = page.locator(".body-sidebar").locator("visible=true").first.bounding_box()["width"]
	box = bottom.bounding_box()
	clip = {"x": left, "y": top, "width": WIDTH - left, "height": box["y"] + box["height"] + pad - top}
	OUT.mkdir(parents=True, exist_ok=True)
	page.screenshot(path=OUT / f"{name}.png", clip=clip)


def open_run(page, run: str):
	"""Open a run's form and wait for its overview. Returns the overview."""
	page.goto(f"/desk/ucm-migration-run/{run}", wait_until="networkidle")
	page.add_style_tag(content=PLAIN)
	overview = page.locator('.frappe-control[data-fieldname="overview"]').locator("visible=true")
	expect(overview.get_by_text("Steps", exact=True)).to_be_visible()
	page.wait_for_timeout(600)
	return overview


def top(page, run: str, name: str) -> None:
	"""The head of a run's form: its state, what it waits for, its steps and the run's facts."""
	overview = open_run(page, run)
	cut(page, name, overview.locator(".row").first, pad=-8)


def steps(page, run: str, name: str, first: str, last: str) -> None:
	"""Some of a run's steps, from one to another, in the width of the list of steps."""
	overview = open_run(page, run)

	def row(label):
		return overview.locator("tr", has=page.get_by_text(label, exact=True)).first.bounding_box()

	column = overview.locator(".row > div").first.bounding_box()
	start, end = row(first), row(last)
	clip = {
		"x": column["x"],
		"y": start["y"] - 8,
		"width": column["width"],
		"height": end["y"] + end["height"] - start["y"] + 16,
	}
	OUT.mkdir(parents=True, exist_ok=True)
	page.screenshot(path=OUT / f"{name}.png", clip=clip)


def findings(page, run: str, name: str) -> None:
	"""A run whose verification found something, down to the list of findings."""
	overview = open_run(page, run)
	cut(page, name, overview.locator("table", has=page.get_by_text("First place in the publication")).first)


def records(page, run: str, name: str) -> None:
	"""The rows of each table on both sides."""
	overview = open_run(page, run)
	table = overview.locator("table", has=page.get_by_text("Published", exact=True)).first
	heading = overview.get_by_text("Records", exact=True).first
	cut(page, name, table, top=heading.bounding_box()["y"] - 10)


def staged(page, run: str, choose: str | None) -> None:
	"""A staged run, its Migration menu and the dialog that chooses the write route."""
	overview = open_run(page, run)
	cut(page, "run-staged", overview.locator(".row").first, pad=-8)
	menu(page)
	shown = page.get_by_role("menu").filter(has_text="Apply hidden batches").locator("visible=true").last
	expect(shown).to_be_visible()
	cut(page, "migration-menu", shown, pad=24)
	entry(page, "Configure write routes").click()
	dialog = page.locator(".modal-dialog:visible")
	expect(dialog.get_by_text("Route for the delegation")).to_be_visible()
	if choose:
		dialog.locator('.frappe-control[data-fieldname="whole"] select').select_option(choose)
		page.wait_for_timeout(400)
	OUT.mkdir(parents=True, exist_ok=True)
	dialog.locator(".modal-content").screenshot(path=OUT / "write-route.png")
	if choose:
		dialog.get_by_role("button", name="Save routes").click()
		expect(page.get_by_text(f"Write route: {choose}")).to_be_visible()
	else:
		dialog.locator(".modal-header .btn-modal-close").click()


def decision(page, run: str, name: str) -> None:
	"""The question the form asks before a delegation is opened. It is answered No."""
	open_run(page, run)
	page.locator(PRIMARY, has_text="Activate delegation").first.click()
	dialog = page.locator(".modal-dialog:visible")
	expect(dialog.get_by_text("Open this delegation to its authorized users")).to_be_visible()
	page.wait_for_timeout(400)
	dialog.locator(".modal-content").screenshot(path=OUT / f"{name}.png")
	dialog.get_by_role("button", name="No").click()


def run_list(page, name: str, rows: int, hide: str | None) -> None:
	"""The list of runs, newest first, without the runs in one state where asked."""
	query = f"?state=%5B%22!%3D%22%2C%22{hide}%22%5D" if hide else ""
	page.goto(f"/desk/ucm-migration-run{query}", wait_until="networkidle")
	listed = page.locator(".list-row-container").locator("visible=true")
	expect(listed.first).to_be_visible()
	page.wait_for_timeout(600)
	cut(page, name, listed.nth(min(rows, listed.count()) - 1), pad=2)


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("shot", choices=["top", "steps", "findings", "records", "staged", "decision", "list"])
	parser.add_argument("--site", required=True)
	parser.add_argument("--run")
	parser.add_argument("--name", help="The picture's file name, without .png")
	parser.add_argument("--choose", help="staged: the route to choose and save in the dialog")
	parser.add_argument("--first", default="Verify", help="steps: the first step to show")
	parser.add_argument("--last", default="Activate", help="steps: the last step to show")
	parser.add_argument("--rows", type=int, default=8, help="list: how many runs to show")
	parser.add_argument("--hide", help="list: leave out the runs in this state")
	args = parser.parse_args()
	if args.shot != "list" and not args.run:
		parser.error("Supply --run")
	with desk(args.site) as page:
		if args.shot == "staged":
			staged(page, args.run, args.choose)
		elif args.shot == "list":
			run_list(page, args.name or "run-list", args.rows, args.hide)
		elif args.shot == "steps":
			steps(page, args.run, args.name or "run-steps", args.first, args.last)
		else:
			shots = {"top": top, "findings": findings, "records": records, "decision": decision}
			shots[args.shot](page, args.run, args.name or f"run-{args.shot}")
	print(f"Saved under {OUT}")


if __name__ == "__main__":
	main()
