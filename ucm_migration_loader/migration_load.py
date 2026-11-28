"""Optional live HTTP latency and current InnoDB lock-wait limits for migration workers."""

import time
from urllib.parse import urlsplit

import frappe
from redis.exceptions import RedisError


def before_request():
	if not frappe.conf.get("ucm_migration_live_p95_ms"):
		return
	path = urlsplit(frappe.request.path).path
	if path.startswith(("/api/resource/", "/api/v2/document/", "/api/method/frappe.desk.form.")):
		frappe.flags.ucm_live_request_started = time.monotonic()


def after_request(request=None, response=None):
	started = frappe.flags.ucm_live_request_started
	if not isinstance(started, (int, float)):
		return
	kind = "read" if request.method in ("GET", "HEAD") or request.path.endswith(".getdoc") else "write"
	key = frappe.cache.make_key("ucm_migration_live_" + kind)
	try:
		now = time.time()
		pipe = frappe.cache.pipeline(transaction=False)
		pipe.lpush(key, f"{now},{(time.monotonic() - started) * 1000:.3f}")
		pipe.ltrim(key, 0, 255)
		pipe.expire(key, 60)
		pipe.execute()
	except RedisError:
		# A monitoring outage must never break the caseworker's request.
		return


def pressure():
	import math

	limit = float(frappe.conf.get("ucm_migration_live_p95_ms") or 0)
	if limit > 0:
		for kind in ("read", "write"):
			samples = (
				x.decode().split(",") for x in frappe.cache.lrange("ucm_migration_live_" + kind, 0, 255)
			)
			values = sorted(float(ms) for ts, ms in samples if float(ts) >= time.time() - 60)
			if values and values[math.ceil(0.95 * len(values)) - 1] > limit:
				return f"Paused: live {kind} p95 exceeded {limit:g} ms"
	maximum = frappe.conf.get("ucm_migration_max_lock_waits")
	if maximum is not None:
		((_, current),) = frappe.db.sql("show global status like 'Innodb_row_lock_current_waits'")
		if int(current) > int(maximum):
			return f"Paused: {current} current database lock waits (limit {maximum})"
