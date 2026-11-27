"""Exercise a consent withdrawal between the source and recipient go-live transactions."""

import frappe
from generator_caseworker_surfaces import published_items
from requests import HTTPError

from ucm_migration_loader import api
from ucm_migration_loader.held_shares import HELD


def assert_denied(request, dt, key):
	try:
		request("GET", f"/api/resource/{dt}/{key}")
	except HTTPError as exc:
		assert exc.response.status_code == 403
	else:
		raise AssertionError("Historical share unexpectedly granted access")


def before(first, recipient, owner_request, recipient_request):
	from ucm.permissions.sharing import SHARE_RECIPIENT

	rows = [
		item["row"]
		for item in published_items(first)
		if item["doctype"] == "Record Share"
		and item["row"]["share_to_context_tech_id"] == recipient
		and item["row"]["share_status"] in ("Accepted", "Pending")
	]
	assert rows
	for row in rows:
		assert frappe.db.get_value(HELD, row["name"], "state") == "Held"
		assert not frappe.db.exists("Record Share", row["name"])
	person = next(
		row for row in rows if row["share_doctype"] == "Person" and row["share_status"] == "Accepted"
	)
	case = next(row for row in rows if row["share_doctype"] == "Case" and row["share_status"] == "Accepted")
	assert_denied(recipient_request, HELD, case["name"])
	assert_denied(recipient_request, "Case", case["share_name"])
	assert_denied(recipient_request, "Person", person["share_name"])
	owner_request(
		"POST",
		"/api/method/ucm.permissions.sharing.set_consent",
		json={
			"person": person["share_name"],
			"recipient": SHARE_RECIPIENT,
			"decision": "Total objection",
		},
	)
	return rows, person, case


def after(first, second, checkpoints, recipient_request):
	from ucm.ucm.doctype.record_share.record_share import shared_contexts

	rows, person, case = checkpoints
	released_pending = 0
	frappe.db.rollback()
	results = {"Released": 0, "Review": 0}
	for row in rows:
		hold = frappe.get_doc(HELD, row["name"])
		assert hold.release_run == second
		results[hold.state] += 1
		assert bool(frappe.db.exists("Record Share", row["name"])) == (hold.state == "Released")
		if hold.state == "Released":
			assert frappe.db.get_value("Record Share", row["name"], "share_status") == row["share_status"]
			if row["share_status"] == "Pending":
				assert row["share_to_context_tech_id"] not in shared_contexts(
					row["share_doctype"], row["share_name"]
				)
				released_pending += 1
		else:
			assert hold.finding == "Current UCM consent prohibits this historical share"
	assert frappe.db.get_value(HELD, person["name"], "state") == "Review"
	assert_denied(recipient_request, "Person", person["share_name"])
	assert (
		recipient_request("GET", "/api/resource/Case/" + case["share_name"])["data"]["name"]
		== case["share_name"]
	)
	# A single source run can still have outstanding shares to another locked context.
	other_holds = [
		item["row"]["name"]
		for item in published_items(first)
		if item["doctype"] == "Record Share"
		and item["row"]["share_status"] in ("Accepted", "Pending")
		and item["row"]["share_to_context_tech_id"]
		and frappe.db.get_value("Context", item["row"]["share_to_context_tech_id"], "context_status")
		!= "Active"
	]
	for key in other_holds:
		assert frappe.db.get_value(HELD, key, "state") == "Held"
		assert not frappe.db.exists("Record Share", key)
	expected = {**results, **({"Held": len(other_holds)} if other_holds else {})}
	assert api.report(first)["held_shares"] == expected
	assert results["Review"] and results["Released"]
	return {
		"accepted_shares_held_before_go_live": sum(r["share_status"] == "Accepted" for r in rows),
		"pending_shares_held_before_go_live": sum(r["share_status"] == "Pending" for r in rows),
		"outcomes": results,
		"released_pending_rows_without_accepted_grant": released_pending,
		"other_inactive_recipient_shares_still_held": len(other_holds),
		"changed_consent_blocked": True,
		"eligible_case_read_after_activation": True,
	}
