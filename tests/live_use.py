"""Measure what caseworkers in live delegations feel while another delegation is loaded beside them.

	env/bin/python apps/ucm_migration_loader/tests/live_use.py --site SITE --users USERS.json \\
		--contexts SAH,BOL --caseworkers 8 --run RUN --publication FILE [--discard-first OLD_RUN]

Run from the bench, on a development site that is being served and has a worker on the migration queue. Each
simulated caseworker is one of the site's own users (the generator's list of them, with their teams and
clearances), with a session of their own. In turn they search persons by name, open a person, open a case, list a
person's history and save a case of their own, with a pause between two requests. The requests are timed during
a quiet baseline and then during each phase of the load: the discard of an earlier run where one is named, the
staging, the writes, the derive, the verification, and an activation that is rolled back.

Each caseworker creates one case to save to, and it is deleted at the end; nothing else of the live delegations
is written. Sessions are opened as `bench browse` does and closed at the end. The result gives, per phase and
kind of request, the number of requests, the errors and the 50th, 95th and 99th percentile in milliseconds,
with the database's lock waits per phase. Synthetic data only.

This is a measurement of interference on a development bench, with a handful of users on its one web process:
it says whether a load disturbs live work and by how much here, not what a production deployment will carry.
"""

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import frappe
import requests

BENCH = Path(__file__).resolve().parents[3]
RUN = "UCM Migration Run"
PAUSE = 0.5  # seconds a caseworker waits between two requests, half as much again at random


class Caseworker(threading.Thread):
	"""One user's session, repeating the same round of requests until told to stop."""

	def __init__(self, user: str, context: str, sid: str, url: str, site: str, meter: "Meter"):
		super().__init__(daemon=True)
		self.user, self.context, self.sid, self.url, self.meter = user, context, sid, url, meter
		self.http = requests.Session()
		self.http.trust_env = False
		self.http.headers["Host"] = site
		self.http.cookies.set("sid", sid)
		self.random = random.Random(user)
		self.persons, self.cases, self.words, self.own_case = [], [], [], None

	def call(self, operation: str | None, method: str, path: str, **kwargs):
		started, phase = time.monotonic(), self.meter.phase
		ok, data = False, None
		try:
			response = self.http.request(method, self.url + path, timeout=60, **kwargs)
			ok = response.status_code == 200
			data = response.json().get("data") if ok else None
		except (requests.RequestException, ValueError):
			pass
		if operation:
			self.meter.add(phase, operation, (time.monotonic() - started) * 1000, ok)
		return data

	def prepare(self) -> None:
		"""What this user works on: persons and cases they can see, and a case of their own to save to."""
		persons = self.call(
			None,
			"GET",
			"/api/resource/Person",
			params={"fields": json.dumps(["name", "person_name"]), "limit_page_length": 60},
		)
		cases = self.call(None, "GET", "/api/resource/Case", params={"limit_page_length": 60})
		if not persons or not cases:
			raise ValueError(f"{self.user} sees no persons or cases")
		self.persons = [row["name"] for row in persons]
		self.cases = [row["name"] for row in cases]
		self.words = sorted({(row["person_name"] or "").split(" ")[0][:4] for row in persons} - {""})
		created = self.call(
			None,
			"POST",
			"/api/resource/Case",
			json={"context_tech_id": self.context, "case_name": "Live-use measurement"},
		)
		if not created:
			raise ValueError(f"{self.user} could not create a case")
		self.own_case = created["name"]

	def run(self) -> None:
		count = 0
		while not self.meter.stop.is_set():
			person = self.random.choice(self.persons)
			word = self.random.choice(self.words)
			count += 1
			for operation, method, path, kwargs in (
				(
					"search persons by name",
					"GET",
					"/api/resource/Person",
					{
						"params": {
							"fields": json.dumps(["name", "person_name", "person_file_id"]),
							"filters": json.dumps([["person_name", "like", f"{word}%"]]),
							"limit_page_length": 20,
						}
					},
				),
				("open a person", "GET", f"/api/resource/Person/{person}", {}),
				("open a case", "GET", f"/api/resource/Case/{self.random.choice(self.cases)}", {}),
				(
					"list a person's history",
					"GET",
					"/api/resource/Activity Detail",
					{
						"params": {
							"filters": json.dumps([["person_tech_id", "=", person]]),
							"limit_page_length": 20,
						}
					},
				),
				(
					"save a case",
					"PUT",
					f"/api/resource/Case/{self.own_case}",
					{"json": {"case_remark": f"Measurement save {count}"}},
				),
			):
				if self.meter.stop.is_set():
					break
				self.call(operation, method, path, **kwargs)
				self.meter.stop.wait(PAUSE * (1 + self.random.random() / 2))


class Meter:
	"""The timed requests, under the phase that was current when each one started."""

	def __init__(self):
		self.stop, self.phase, self.samples, self.phases = threading.Event(), "baseline", [], {}
		self.started = time.monotonic()
		self.locks = self._locks()

	def add(self, phase: str, operation: str, milliseconds: float, ok: bool) -> None:
		self.samples.append((phase, operation, milliseconds, ok))

	def _locks(self) -> dict:
		frappe.db.rollback()
		rows = frappe.db.sql(
			"show global status where Variable_name in"
			" ('Innodb_row_lock_waits', 'Innodb_row_lock_time', 'Innodb_deadlocks')"
		)
		return {name: int(value) for name, value in rows}

	def enter(self, phase: str) -> None:
		"""Close the current phase and open another."""
		now, locks = time.monotonic(), self._locks()
		closed = self.phases.setdefault(
			self.phase, {"seconds": 0.0, "row_lock_waits": 0, "row_lock_ms": 0, "deadlocks": 0}
		)
		closed["seconds"] += now - self.started
		closed["row_lock_waits"] += locks["Innodb_row_lock_waits"] - self.locks["Innodb_row_lock_waits"]
		closed["row_lock_ms"] += locks["Innodb_row_lock_time"] - self.locks["Innodb_row_lock_time"]
		closed["deadlocks"] += locks.get("Innodb_deadlocks", 0) - self.locks.get("Innodb_deadlocks", 0)
		self.phase, self.started, self.locks = phase, now, locks

	def summary(self) -> dict:
		grouped = defaultdict(list)
		for phase, operation, milliseconds, ok in self.samples:
			grouped[phase, operation].append((milliseconds, ok))
			grouped[phase, "all requests"].append((milliseconds, ok))
		out = {}
		for phase, facts in self.phases.items():
			out[phase] = {**{key: round(value, 1) for key, value in facts.items()}, "requests": {}}
		out.pop("end", None)
		for (phase, operation), values in sorted(grouped.items()):
			times = sorted(ms for ms, ok in values if ok)
			row = {"requests": len(values), "errors": len(values) - len(times)}
			for percentile in (50, 95, 99):
				row[f"p{percentile}_ms"] = (
					round(times[math.ceil(len(times) * percentile / 100) - 1]) if times else None
				)
			row["max_ms"] = round(times[-1]) if times else None
			out.setdefault(phase, {"requests": {}})["requests"][operation] = row
		return out


def record(run: str):
	frappe.db.rollback()  # a new read of the run, past this connection's snapshot
	return frappe.get_doc(RUN, run)


def sessions(users: list[tuple[str, str]]) -> dict:
	"""A session for each user, opened from the server as `bench browse` opens one."""
	from frappe.auth import CookieManager, LoginManager

	opened = {}
	for user, _context in users:
		frappe.utils.set_request(path="/")
		frappe.local.cookie_manager = CookieManager()
		frappe.local.login_manager = LoginManager()
		frappe.local.login_manager.login_as(user)
		opened[user] = frappe.session.sid
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: a rehearsal run from the bench by its operator; the loader's commands require a System Manager
	frappe.set_user("Administrator")
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the web server is another process, and sees only what is committed
	frappe.db.commit()
	return opened


def choose(users_file: Path, contexts: list[str], number: int) -> list[tuple[str, str]]:
	"""Enabled users of the named delegations, of as many kinds as there are, with their context's key."""
	keys = {code: frappe.db.get_value("Context", {"context_code": code}, "name") for code in contexts}
	by_kind = defaultdict(list)
	for user in json.loads(users_file.read_text()):
		if user.get("context") in keys and frappe.db.get_value("User", user["email"], "enabled"):
			by_kind[user["context"], user["kind"]].append(user["email"])
	# by kind first, so that the delegations take turns
	chosen, groups = [], sorted(by_kind.items(), key=lambda group: (group[0][1], group[0][0]))
	while groups and len(chosen) < number:
		for (context, _kind), emails in groups:
			if emails and len(chosen) < number:
				chosen.append((emails.pop(0), keys[context]))
		groups = [group for group in groups if group[1]]
	return chosen


def follow(meter: Meter, run: str, going: tuple, label, timeout: float) -> dict:
	"""Name the phases after what the worker is doing, until it has left the run."""
	started, seen = time.monotonic(), None
	while time.monotonic() - started < timeout:
		current = record(run)
		phase = label(current)
		if phase != seen:
			meter.enter(phase)
			seen = phase
		if current.state not in going or current.finding:
			break
		time.sleep(1)
	current = record(run)
	return {
		"state": current.state,
		"finding": current.finding,
		"seconds": round(time.monotonic() - started, 1),
	}


def load_phase(current) -> str:
	phase = current.worker_phase or ""
	if current.state != "Publication applying":
		return "after the load"
	if phase.startswith("Writing") or not phase or phase == "Discarded":
		return "load: write"
	if phase == "Deriving":
		# the phase is marked when its job starts: the derive, then the first verified batches
		return "load: derive"
	return "load: verify"


def measure(args, publication: Path | None) -> dict:
	from ucm_migration_loader import api, write_routes
	from ucm_migration_loader.publication_receiver import stage
	from ucm_migration_loader.ucm_migration_loader import set_based

	users = choose(args.users, args.contexts.split(","), args.caseworkers)
	if len(users) < args.caseworkers:
		raise ValueError("The users file names too few enabled users of these delegations")
	opened = sessions(users)
	meter = Meter()
	url = f"http://127.0.0.1:{frappe.conf.webserver_port or 8000}"
	workers = [Caseworker(user, context, opened[user], url, args.site, meter) for user, context in users]
	out = {"caseworkers": [[user, context] for user, context in users], "steps": {}}
	try:
		for worker in workers:
			worker.prepare()
		meter.started = time.monotonic()
		for worker in workers:
			worker.start()
		time.sleep(args.baseline)
		if args.discard_first:
			meter.enter("discard of an earlier run")
			api.discard(args.discard_first)
			frappe.db.commit()
			out["steps"]["discard"] = follow(
				meter,
				args.discard_first,
				("Publication cleaning",),
				lambda current: "discard of an earlier run",
				7200,
			)
		if args.run:
			meter.enter("staging")
			started = time.monotonic()
			if publication:
				stage(str(publication), args.run)
				frappe.db.commit()
			write_routes.select(args.run, write_routes.SET_BASED)
			frappe.db.commit()
			out["steps"]["stage"] = {"seconds": round(time.monotonic() - started, 1)}
			meter.enter("load: write")
			api.apply(args.run)
			frappe.db.commit()
			out["steps"]["load"] = follow(meter, args.run, ("Publication applying",), load_phase, 14400)
			if record(args.run).state == "Publication ready":
				meter.enter("activation, rolled back")
				result = set_based.activate(args.run, commit=False)
				out["steps"]["activation"] = {
					key: result[key]
					for key in ("released_access_rows", "shares_held_at_activation", "seconds")
				}
		meter.enter("quiet again")
		time.sleep(args.baseline / 2)
		meter.stop.set()
		meter.enter("end")
	finally:
		meter.stop.set()
		for worker in workers:
			worker.join(timeout=90)
		frappe.db.rollback()
		from frappe.sessions import delete_session

		for worker in workers:
			if worker.own_case:
				frappe.delete_doc("Case", worker.own_case, force=True, ignore_permissions=True)
			delete_session(worker.sid, reason="End of the measurement")
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the measurement's own cases and sessions end with it
		frappe.db.commit()
	return {**out, "phases": meter.summary()}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--site", required=True)
	parser.add_argument("--users", required=True, type=Path, help="The generator's users.json of the site")
	parser.add_argument(
		"--contexts", required=True, help="Codes of the live delegations the caseworkers work in"
	)
	parser.add_argument("--caseworkers", type=int, default=8)
	parser.add_argument("--baseline", type=float, default=120, help="Seconds of quiet measurement first")
	parser.add_argument("--discard-first", help="A run to discard under the same traffic, before the load")
	parser.add_argument("--run", help="The run to load; without one only the baseline is measured")
	parser.add_argument("--publication", type=Path, help="Stage this sealed file for the run first")
	parser.add_argument("--evidence", type=Path, help="Write the result here as well")
	args = parser.parse_args()
	args.users = args.users.resolve()
	publication = args.publication and args.publication.resolve()
	evidence = args.evidence and args.evidence.resolve()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: a rehearsal run from the bench by its operator; the loader's commands require a System Manager
	frappe.set_user("Administrator")
	try:
		result = measure(args, publication)
	finally:
		frappe.destroy()
	text = json.dumps(result, indent=1, default=str)
	print(text)
	if evidence:
		evidence.write_text(text + "\n")


if __name__ == "__main__":
	sys.exit(main())
