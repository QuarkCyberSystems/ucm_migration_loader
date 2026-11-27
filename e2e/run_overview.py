"""The migration team in Desk, in a browser: the list of runs, and the form of a run in each situation it has to
show.

	env/bin/python apps/ucm_migration_loader/e2e/run_overview.py --site <site> \
		[--ready <run>] [--findings <run>] [--refused <run>] [--activated <run>] \
		[--dashboard <run>] [--load <run>] [--activate <run>]

Run from the bench, on a development site that is being served. `--ready`, `--findings`, `--refused` and
`--activated` only read runs that `tests/run_overview_site.py` and `tests/set_based_queue.py` brought into these
situations. `--dashboard` sets the site's `ucm_migration_quality_url` for the time of its check. Two checks act,
and need a worker on the migration queue: `--load` applies a staged run and `--activate` opens the delegation of a
ready one, each asked for on the form, which must then show the result by itself. A session is opened for the
Administrator, as `bench browse` does, and closed at the end. Needs Playwright and Chromium. Synthetic data only.
"""

import argparse
import pathlib
import sys
import time

from playwright.sync_api import expect, sync_playwright
from set_based_route import action, close, session

from ucm_migration_loader import runs

SHOTS = pathlib.Path(__file__).resolve().parent / "screenshots"
TALL = 2000  # the Desk scrolls inside the page: a tall window shows a whole form in one screenshot
SETTING = "ucm_migration_quality_url"
PRIMARY = ".page-actions button.btn-primary"
DASHBOARD = "Open in data quality dashboard"


def open_run(page, run: str):
	"""Open a run's form and wait for its overview."""
	page.goto(f"/desk/ucm-migration-run/{run}", wait_until="networkidle")
	overview = page.locator('.frappe-control[data-fieldname="overview"]').locator("visible=true")
	expect(overview.get_by_text("Steps", exact=True)).to_be_visible()
	return overview


def step(overview, label: str):
	"""The row of one step in the overview's list."""
	return overview.locator("tr", has=overview.page.get_by_text(label, exact=True)).first


def shot(page, name: str) -> None:
	page.screenshot(path=SHOTS / name)


def ready(page, run: str) -> None:
	overview = open_run(page, run)
	expect(page.locator(".form-message").get_by_text("All checks passed.")).to_be_visible()
	for label in ("Check and stage", "Write", "Verify"):
		expect(step(overview, label).get_by_text("Done", exact=True)).to_be_visible()
	expect(step(overview, "Verify").get_by_text("nothing found")).to_be_visible()
	expect(step(overview, "Activate").get_by_text("Decision", exact=True)).to_be_visible()
	expect(overview.get_by_text("Records", exact=True)).to_be_visible()
	expect(overview.get_by_text("held until activation").first).to_be_visible()
	expect(overview.get_by_text("Findings", exact=True)).to_have_count(0)
	# the decision the run waits for is a button of its own
	expect(page.locator(PRIMARY, has_text="Activate delegation").first).to_be_visible()
	shot(page, "3-waiting-for-decision.png")


def findings(page, run: str) -> None:
	overview = open_run(page, run)
	expect(page.locator(".form-message").get_by_text("cannot be activated and stays hidden")).to_be_visible()
	expect(step(overview, "Verify").get_by_text("Failed", exact=True)).to_be_visible()
	expect(step(overview, "Activate").get_by_text("Waiting", exact=True)).to_be_visible()
	table = overview.locator("table", has=page.get_by_text("First place in the publication"))
	expect(table.get_by_text("Critical", exact=True).first).to_be_visible()
	expect(table.get_by_text("High", exact=True).first).to_be_visible()
	expect(table.get_by_text("Medium", exact=True).first).to_be_visible()
	expect(table.get_by_text("Batch ", exact=False).first).to_be_visible()
	expect(page.locator(PRIMARY, has_text="Activate delegation")).to_have_count(0)
	shot(page, "4-verification-failed.png")


def refused(page, run: str) -> None:
	overview = open_run(page, run)
	expect(page.locator(".form-message").get_by_text("Preflight refused")).to_be_visible()
	expect(step(overview, "Check and stage").get_by_text("Failed", exact=True)).to_be_visible()
	expect(step(overview, "Check and stage").get_by_text("Record IDs already on this site")).to_be_visible()
	expect(overview.get_by_text("Write", exact=True)).to_have_count(0)
	shot(page, "5-refused.png")


def activated(page, run: str) -> None:
	overview = open_run(page, run)
	expect(page.locator(".form-message").get_by_text("is open to its users")).to_be_visible()
	expect(step(overview, "Activate").get_by_text("Done", exact=True)).to_be_visible()
	expect(step(overview, "Activate").get_by_text("assignments and shares released")).to_be_visible()
	# the access rows are counted now, and nothing is left to decide or to discard
	expect(overview.get_by_text("held until activation")).to_have_count(0)
	expect(page.locator(PRIMARY, has_text="Activate delegation")).to_have_count(0)
	expect(page.get_by_text("Discard run", exact=True)).to_have_count(0)
	shot(page, "7-activated.png")


def dashboard_button(page, run: str, shown: bool):
	"""The form's link to the dashboard, once it is (or is no longer) there: a running site reads its settings
	again within a minute."""
	button = page.locator(".page-actions button", has_text=DASHBOARD)
	deadline = time.monotonic() + 90
	while True:
		open_run(page, run)
		if (button.count() > 0) == shown:
			return button.first
		assert time.monotonic() < deadline, f"the dashboard link is {'missing' if shown else 'still shown'}"
		page.wait_for_timeout(5000)


def dashboard(page, run: str, base_url: str) -> None:
	"""With the site's setting, the form links to the run in the migration side's dashboard. The setting is this
	check's own: it is removed at the end, and the link with it."""
	from frappe.installer import update_site_config

	update_site_config(SETTING, f"{base_url}/api/method/ping?run={{run}}&context={{context}}")
	try:
		button = dashboard_button(page, run, shown=True)
		with page.context.expect_page() as opened:
			button.click()
		assert f"/api/method/ping?run={run}&context=" in opened.value.url, opened.value.url
		opened.value.close()
	finally:
		update_site_config(SETTING, "None")
	dashboard_button(page, run, shown=False)


def stay(page) -> None:
	"""Mark the page: loading it again would lose the mark."""
	page.evaluate("() => { window.ucm_same_page = true }")


def stayed(page) -> None:
	assert page.evaluate("() => window.ucm_same_page === true"), "the page was loaded again"


def load(page, run: str, minutes: float) -> None:
	"""Ask for the load on the form, and leave the form alone: it shows the steps and the result by itself."""
	overview = open_run(page, run)
	expect(step(overview, "Write").get_by_text("Waiting", exact=True)).to_be_visible()
	stay(page)
	action(page, "Apply hidden batches")
	expect(step(overview, "Activate").get_by_text("Decision", exact=True)).to_be_visible(
		timeout=minutes * 60_000
	)
	for label in ("Write", "Verify"):
		expect(step(overview, label).get_by_text("Done", exact=True)).to_be_visible()
	expect(page.locator(".form-message").get_by_text("All checks passed.")).to_be_visible()
	expect(page.locator(PRIMARY, has_text="Activate delegation").first).to_be_visible()
	stayed(page)
	shot(page, "8-loaded.png")


def activate(page, run: str, minutes: float) -> None:
	"""Take the go-live decision on the form: it shows the open delegation once the worker has finished."""
	overview = open_run(page, run)
	stay(page)
	page.locator(PRIMARY, has_text="Activate delegation").first.click()
	dialog = page.locator(".modal-dialog:visible")
	expect(dialog.get_by_text("Open this delegation to its authorized users")).to_be_visible()
	dialog.get_by_role("button", name="Yes").click()
	expect(page.locator(".form-message").get_by_text("is open to its users")).to_be_visible(
		timeout=minutes * 60_000
	)
	expect(step(overview, "Activate").get_by_text("Done", exact=True)).to_be_visible()
	expect(page.locator(PRIMARY, has_text="Activate delegation")).to_have_count(0)
	stayed(page)
	shot(page, "9-activated-on-the-form.png")


def run_list(page) -> None:
	page.goto("/desk/ucm-migration-run", wait_until="networkidle")
	rows = page.locator(".list-row-container").locator("visible=true")
	expect(rows.first).to_be_visible()
	for column in ("Delegation", "Route", "Records", "Duration"):
		expect(page.locator(".list-row-head").get_by_text(column, exact=True).first).to_be_visible()
	expect(rows.get_by_text("Set-based SQL", exact=True).first).to_be_visible()
	shot(page, "6-runs.png")


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--site", required=True)
	for situation in ("ready", "findings", "refused", "activated", "dashboard", "load", "activate"):
		parser.add_argument(f"--{situation}", metavar="RUN")
	parser.add_argument("--minutes", type=float, default=30, help="How long to wait for the worker")
	args = parser.parse_args()
	SHOTS.mkdir(exist_ok=True)
	sid, port = session(args.site)
	base_url = f"http://{args.site}:{port}"
	checks = {
		"ready": ready,
		"findings": findings,
		"refused": refused,
		"activated": activated,
		"dashboard": lambda page, run: dashboard(page, run, base_url),
		"load": lambda page, run: load(page, run, args.minutes),
		"activate": lambda page, run: activate(page, run, args.minutes),
	}
	errors, done = [], []
	try:
		with sync_playwright() as playwright:
			browser = playwright.chromium.launch(args=[f"--host-resolver-rules=MAP {args.site} 127.0.0.1"])
			context = browser.new_context(base_url=base_url, viewport={"width": 1400, "height": TALL})
			context.add_cookies([{"name": "sid", "value": sid, "domain": args.site, "path": "/"}])
			page = context.new_page()
			page.on("pageerror", lambda error: errors.append(str(error)))
			try:
				for name, check in checks.items():
					if run := getattr(args, name):
						check(page, run)
						done.append(name)
				run_list(page)
			except Exception:
				page.screenshot(path=SHOTS / "failure.png", full_page=True)
				raise
			finally:
				browser.close()
	finally:
		close(sid)
	if errors:
		sys.exit(f"The page raised errors: {errors[:3]}")
	print("The run list and the forms showed what the runs hold: " + ", ".join([*done, "list"]))


if __name__ == "__main__":
	main()
