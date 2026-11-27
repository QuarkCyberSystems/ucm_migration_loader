"""Commit, interrupt, replay, cancel and activate synthetic hidden business batches."""

import argparse
import json
import multiprocessing
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import frappe
from first_wave import caseworker_session
from rehearse import call, finish, make_wave, stage, worker
from requests import HTTPError
from transport import BENCH

from ucm_migration_loader import api, application


def interrupt_before_receipt(site, run):
	"""End a disposable child process after the ORM insert, before its receipt and DB commit."""
	frappe.init(site=site)
	frappe.connect()
	original = frappe.db.set_value

	def stop(doctype, *args, **kwargs):
		if doctype == api.BATCH:
			os._exit(71)
		return original(doctype, *args, **kwargs)

	with patch.object(frappe.db, "set_value", stop):
		application.apply_batch(run, 2, 2, frappe.db.get_value(api.RUN, run, "dispatch_id"))


def wait_verified(manifest, batches):
	run = stage(manifest, batches)
	call("verify", run=run)
	worker()
	assert call("status", run=run)["state"] == "Verified"
	return run


def assert_hidden(request, dt, name):
	assert request("GET", f"/api/resource/{dt}", params={"filters": json.dumps({"name": name})})["data"] == []
	try:
		request("GET", f"/api/resource/{dt}/{name}")
	except HTTPError as error:
		assert error.response.status_code == 403
	else:
		raise AssertionError(f"Caseworker can open the hidden {dt}")


def exercise(site, folder):
	from ucm.tests.utils import make_user

	a_ids, a_manifest, a_batches = make_wave(site, "synthetic-live")
	finish(stage(a_manifest, a_batches), a_ids)
	ids, manifest, batches = make_wave(site, "synthetic-batch-recovery")
	context = manifest["context"]
	user = make_user(
		f"ucm-recovery-{uuid4().hex}@example.invalid",
		[(a_manifest["context"], "Write"), (context, "Write")],
	)
	frappe.db.commit()
	try:
		with caseworker_session(user, folder) as request:
			run = wait_verified(manifest, batches)
			call("apply", run=run, max_batches=1)
			worker()
			assert call("status", run=run)["applied"] == 1
			assert frappe.db.exists("Person", ids["person"]) and frappe.db.exists(
				"Person Other Name", ids["other"]
			)
			assert_hidden(request, "Person", ids["person"])
			call("apply", run=run, max_batches=1)
			child = multiprocessing.get_context("spawn").Process(
				target=interrupt_before_receipt, args=(site, run)
			)
			child.start()
			child.join(45)
			if child.is_alive():
				child.terminate()
				child.join()
			assert child.exitcode == 71, child.exitcode
			frappe.db.rollback()
			assert call("status", run=run)["applied"] == 1 and not frappe.db.exists("Case", ids["case"])
			assert not frappe.db.get_value(api.BATCH, f"{run}-2", "target_digest")
			assert call("pause", run=run)["state"] == "Paused"
			worker()
			assert call("status", run=run)["applied"] == 1
			call("apply", run=run, max_batches=1)
			worker()
			assert call("status", run=run)["applied"] == 2
			application.apply_batch(run, 2, 2, frappe.db.get_value(api.RUN, run, "dispatch_id"))
			frappe.db.commit()
			assert_hidden(request, "Case", ids["case"])
			changed = request(
				"PUT",
				"/api/resource/Case/" + a_ids["case"],
				json={"case_remark": "Saved between delegation B commits"},
			)
			assert changed["data"]["case_remark"] == "Saved between delegation B commits"

			def reopen():
				ctx = frappe.get_doc("Context", context)
				ctx.context_status = "Active"
				ctx.save()

			def share():
				frappe.get_doc(
					{
						"doctype": "Record Share",
						"context_tech_id": context,
						"share_doctype": "Case",
						"share_name": ids["case"],
						"share_to_user": user,
						"share_status": "Accepted",
					}
				).insert()

			for action in (reopen, share):
				try:
					action()
				except frappe.ValidationError as error:
					assert "Activate or discard" in str(error), error
					frappe.db.rollback()
				else:
					raise AssertionError("Hidden context was reopened or shared")
			call("apply", run=run)
			worker()
			assert call("status", run=run)["state"] == "Ready"
			assert_hidden(request, "Case Member", ids["member"])
			assert call("discard", run=run)["state"] == "Cleaning"
			worker()
			assert call("status", run=run)["state"] == "Discarded"
			for dt, key in (
				("Case Member", "member"),
				("Case", "case"),
				("Person", "person"),
				("Person Other Name", "other"),
			):
				assert not frappe.db.exists(dt, ids[key])
			retry = wait_verified({**manifest, "attempt": 2}, batches)
			call("apply", run=retry)
			worker()
			ready = call("status", run=retry)
			assert ready["applied"] == 3 and ready["state"] == "Ready", ready
			report = call("report", run=retry)["reconciliation"]
			assert all(x["rows"] == 1 for x in report["entities"].values())
			frappe.db.set_value(
				"Case",
				ids["case"],
				"case_name",
				"Synthetic drift",
				update_modified=False,
			)
			frappe.db.commit()
			call("activate", run=retry)
			worker()
			held = call("status", run=retry)
			assert held["state"] == "Ready" and held["finding"], held
			assert_hidden(request, "Case", ids["case"])
			frappe.db.set_value("Case", ids["case"], "case_name", "Pilot Case", update_modified=False)
			frappe.db.commit()
			call("activate", run=retry)
			worker()
			assert call("status", run=retry)["state"] == "Activated"
			assert (
				request("GET", "/api/resource/Case/" + ids["case"])["data"]["case_number"]
				== "SYN-" + ids["case"][:8]
			)
			assert (
				request("GET", "/api/resource/Case/" + a_ids["case"])["data"]["case_remark"]
				== changed["data"]["case_remark"]
			)
			return {
				"site": site,
				"interrupted_batch": 2,
				"cursor_after_crash": 1,
				"discarded": run,
				"activated": retry,
			}
	finally:
		frappe.db.rollback()
		account = frappe.get_doc("User", user)
		account.enabled = 0
		account.save()
		frappe.db.commit()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	args = parser.parse_args()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory(prefix="ucm-recovery-") as folder:
			print(json.dumps(exercise(args.site, Path(folder))))
	finally:
		frappe.destroy()
