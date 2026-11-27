"""Receiver-owned write plans for sealed delegation publications."""

import json

import frappe

from . import api
from .frozen import ORDER

DOCUMENT = "Document API"
SQL = "Validated bulk SQL"
# Writes, derives and verifies a delegation in sets: a route for the whole run, never for some DocTypes of it.
SET_BASED = "Set-based SQL"
ROUTES = (DOCUMENT, SQL, SET_BASED)


def parent_types(manifest):
	counts = manifest["counts"]
	return [dt for dt in ORDER if counts.get(dt, 0)]


def sql_allowed(dt):
	return dt in ORDER


def route_for(plan, dt):
	route = plan.get(dt, DOCUMENT)
	if route == DOCUMENT or (route == SQL and sql_allowed(dt)):
		return route
	raise ValueError(f"Write route is unavailable for {dt}")


def set_based(plan):
	"""Whether a run's plan is the set-based route. A plan that mixes it with another route is refused."""
	chosen = [route == SET_BASED for route in plan.values()]
	if any(chosen) and not all(chosen):
		raise ValueError("The set-based route applies to every DocType of a run")
	return any(chosen)


def default_plan(manifest):
	"""The plan a staged run starts with: the site's `ucm_migration_route`, or the document route."""
	route = frappe.conf.get("ucm_migration_route") or DOCUMENT
	if route not in ROUTES:
		raise ValueError("Site setting ucm_migration_route names an unknown write route")
	return dict.fromkeys(parent_types(manifest), route)


def editable(record):
	return record.state == "Publication staged" and not record.applied and not record.dispatch_id


@frappe.whitelist(methods=["POST"])
def options(run: str):
	api._operator()
	record = api._record(run)
	if not editable(record):
		raise ValueError("Write routes are locked once apply starts")
	plan = json.loads(record.write_plan_json or "{}")
	return [
		{
			"doctype": dt,
			"selected": plan.get(dt, DOCUMENT),
			"sql_allowed": sql_allowed(dt),
		}
		for dt in parent_types(json.loads(record.manifest_json))
	]


@frappe.whitelist(methods=["POST"])
def configure(run: str, routes: dict | str):
	api._operator()
	record = api._record(run)
	if not editable(record):
		raise ValueError("Write routes are locked once apply starts")
	if isinstance(routes, str):
		routes = json.loads(routes)
	if not isinstance(routes, dict) or set(routes) != set(parent_types(json.loads(record.manifest_json))):
		raise ValueError("Choose a write route for every published parent DocType")
	if not set_based(routes):
		for dt in routes:
			route_for(routes, dt)
	record.write_plan_json = json.dumps(routes, sort_keys=True)
	record.save(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def select(run: str, route: str):
	"""Choose one route for every published DocType of a staged run."""
	if route not in ROUTES:
		raise ValueError("Unknown write route")
	record = api._record(run)
	configure(run, dict.fromkeys(parent_types(json.loads(record.manifest_json)), route))
	return {"run": run, "route": route}
