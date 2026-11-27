"""An operator in Desk, in a browser: choose the set-based route for a staged run, apply it, and wait until the
bench's own worker has made it ready.

	env/bin/python apps/ucm_migration_loader/e2e/set_based_route.py --site <site> --run <run digest>

Run from the bench, on a development site that is being served and has a worker on the migration queue. The run
must be staged and not yet applied; it is left ready and hidden. A session is opened for the Administrator, as
`bench browse` does, and closed at the end. Needs Playwright and Chromium (pip install playwright; playwright
install chromium). Synthetic data only.
"""

import argparse
import os
import pathlib
import sys
import time

import frappe
from playwright.sync_api import expect, sync_playwright

BENCH = pathlib.Path(__file__).resolve().parents[3]
SHOTS = pathlib.Path(__file__).resolve().parent / "screenshots"
MENU = "Migration"


def session(site: str) -> tuple[str, int]:
	from frappe.auth import CookieManager, LoginManager

	os.chdir(BENCH / "sites")
	frappe.init(site=site)
	frappe.connect()
	frappe.utils.set_request(path="/")
	frappe.local.cookie_manager = CookieManager()
	frappe.local.login_manager = LoginManager()
	frappe.local.login_manager.login_as("Administrator")
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the web server is another process, and sees only what is committed
	frappe.db.commit()
	return frappe.session.sid, int(frappe.conf.webserver_port or 8000)


def close(sid: str) -> None:
	from frappe.sessions import delete_session

	delete_session(sid, reason="End of the browser test")
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the test's session ends with the test
	frappe.db.commit()
	frappe.destroy()


def menu(page):
	"""Open the form's Migration menu."""
	page.locator(".inner-group-button", has_text=MENU).locator("visible=true").first.click()


def entry(page, label: str):
	return page.get_by_text(label, exact=True).locator("visible=true").first


def action(page, label: str) -> None:
	"""Choose an entry of the form's Migration menu."""
	menu(page)
	entry(page, label).click()


def run(page) -> dict:
	"""The run as the form holds it."""
	return page.evaluate(
		"() => ({state: cur_frm.doc.state, phase: cur_frm.doc.worker_phase || '',"
		" applied: cur_frm.doc.applied || 0, verified: cur_frm.doc.reconcile_cursor || 0})"
	)


def choose_route(page) -> None:
	action(page, "Configure write routes")
	dialog = page.locator(".modal-dialog:visible")
	expect(dialog.get_by_text("Route for the delegation")).to_be_visible()
	per_doctype = dialog.locator('.frappe-control[data-fieldname="route_0"]')
	expect(per_doctype).to_be_visible()
	dialog.locator('.frappe-control[data-fieldname="whole"] select').select_option("Set-based SQL")
	# the routes per DocType give way to the one route of the delegation
	expect(per_doctype).to_be_hidden()
	page.screenshot(path=SHOTS / "1-route.png")
	dialog.get_by_role("button", name="Save routes").click()
	expect(page.get_by_text("Write route: Set-based SQL")).to_be_visible()


def apply(page, url: str, minutes: float) -> list:
	"""Ask for the load, and follow the form until the worker has left the run ready."""
	action(page, "Apply hidden batches")
	started, seen = time.monotonic(), []
	while time.monotonic() - started < minutes * 60:
		page.goto(url, wait_until="networkidle")
		current = run(page)
		if not seen or (seen[-1][1]["state"], seen[-1][1]["phase"]) != (current["state"], current["phase"]):
			seen.append((round(time.monotonic() - started), current))
		if current["state"] != "Publication applying":
			break
		page.wait_for_timeout(3000)
	assert run(page)["state"] == "Publication ready", f"The run ended as {run(page)['state']}"
	expect(page.get_by_text("Ready for activation", exact=True).first).to_be_visible()
	menu(page)
	expect(entry(page, "Activate delegation")).to_be_visible()
	expect(entry(page, "Download report")).to_be_visible()
	page.screenshot(path=SHOTS / "2-ready.png")
	return seen


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--site", required=True)
	parser.add_argument("--run", required=True)
	parser.add_argument("--minutes", type=float, default=30, help="How long to wait for the worker")
	args = parser.parse_args()
	SHOTS.mkdir(exist_ok=True)
	sid, port = session(args.site)
	errors = []
	try:
		with sync_playwright() as playwright:
			# A bench tells its sites apart by the host name: the browser is told where that name lives, since
			# a development site's name is often known to no resolver
			browser = playwright.chromium.launch(args=[f"--host-resolver-rules=MAP {args.site} 127.0.0.1"])
			context = browser.new_context(
				base_url=f"http://{args.site}:{port}", viewport={"width": 1400, "height": 1000}
			)
			context.add_cookies([{"name": "sid", "value": sid, "domain": args.site, "path": "/"}])
			page = context.new_page()
			page.on("pageerror", lambda error: errors.append(str(error)))
			url = f"/desk/ucm-migration-run/{args.run}"
			try:
				page.goto(url, wait_until="networkidle")
				assert run(page)["state"] == "Publication staged", (
					f"The run is {run(page)['state']}, not staged"
				)
				choose_route(page)
				seen = apply(page, url, args.minutes)
				print(f"Ready {seen[-1][0]} s after the operator asked for the load")
				for at, current in seen:
					print(
						f"  +{at} s  {current['state']}  {current['phase']}"
						f"  written {current['applied']}  verified {current['verified']}"
					)
			except Exception:
				page.screenshot(path=SHOTS / "failure.png")
				raise
			finally:
				browser.close()
	finally:
		close(sid)
	if errors:
		sys.exit(f"The page raised errors: {errors[:3]}")
	print("The operator chose the set-based route and applied the run in the browser")


if __name__ == "__main__":
	main()
