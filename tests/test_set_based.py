"""The parts of the set-based route that need no site: how batches are grouped into transactions, where the
business batches end, and when a stored value equals a published one."""

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from ucm_migration_loader import write_routes
from ucm_migration_loader.ucm_migration_loader import set_based


def publication(*batches):
	"""An in-memory publication holding batches of (doctype, records)."""
	db = sqlite3.connect(":memory:")
	db.execute(
		"create table batches (sequence integer primary key, sha text not null, payload blob not null)"
	)
	for doctype, records in batches:
		payload = json.dumps(
			[{"doctype": doctype, "row": {"name": str(n)}, "children": {}} for n in range(records)]
		)
		db.execute("insert into batches (sha, payload) values (?, ?)", ("sha", payload.encode()))
	return db


def batches(db, after=0, upto=None):
	"""The batches a write step reads: after the last committed one, up to the last business batch."""
	upto = set_based._business_batches(db) if upto is None else upto
	return db.execute(
		"select sequence, sha, payload from batches where sequence > ? and sequence <= ? order by sequence",
		(after, upto),
	)


def shape(groups):
	return [(doctype, [sequence for sequence, _sha, _items, _size in group]) for doctype, group in groups]


def test_batches_of_one_doctype_share_a_transaction():
	db = publication(("Person", 3), ("Person", 3), ("Case", 2), ("ToDo", 1), ("Record Share", 1))
	assert shape(set_based._groups(batches(db))) == [("Person", [1, 2]), ("Case", [3])]


def test_a_transaction_closes_at_the_row_limit(monkeypatch):
	monkeypatch.setattr(set_based, "COMMIT_ROWS", 5)
	db = publication(("Person", 3), ("Person", 3), ("Person", 3))
	assert shape(set_based._groups(batches(db))) == [("Person", [1, 2]), ("Person", [3])]


def test_apply_resumes_after_the_last_committed_batch_and_stops_where_asked():
	db = publication(("Person", 1), ("Person", 1), ("Case", 1), ("Activity", 1))
	assert shape(set_based._groups(batches(db, 1, 3))) == [("Person", [2]), ("Case", [3])]


def test_a_group_carries_the_size_of_each_batch_for_its_receipt():
	db = publication(("Person", 2))
	((_doctype, group),) = set_based._groups(batches(db))
	(payload,) = db.execute("select payload from batches").fetchone()
	assert group[0][3] == len(payload)


def test_a_doctype_outside_the_profile_is_refused(monkeypatch):
	monkeypatch.setattr(set_based, "_", str)  # no site here to translate the message for
	db = publication(("Person", 1), ("User", 1))
	with pytest.raises(ValueError):
		list(set_based._groups(batches(db)))


def test_an_assignment_before_the_last_business_batch_is_refused(monkeypatch):
	monkeypatch.setattr(set_based, "_", str)
	db = publication(("Person", 1), ("ToDo", 1), ("Case", 1))
	with pytest.raises(ValueError):
		list(set_based._groups(batches(db)))


class Run(dict):
	"""A run record, as far as the steps read it."""

	__getattr__ = dict.get


def test_a_job_holds_a_run_until_it_is_paused_or_taken():
	assert set_based._mine(Run(state=set_based.APPLYING, dispatch_id=4), 4)
	assert set_based._mine(Run(state=set_based.STAGED, dispatch_id=4), None)
	assert not set_based._mine(Run(state=set_based.APPLYING, dispatch_id=5), 4)
	assert not set_based._mine(Run(state=set_based.PAUSED, dispatch_id=4), 4)
	assert not set_based._mine(Run(state=set_based.CLEANING, dispatch_id=4), None)


def test_progress_of_another_route_is_not_taken_over():
	kept = {"route": set_based.ROUTE, "derived": {"place geography": 0}}
	assert set_based._progress(Run(reconcile_counts=json.dumps(kept))) == kept
	assert set_based._progress(Run(reconcile_counts=json.dumps({"checked": {}}))) == {
		"route": set_based.ROUTE
	}
	assert set_based._progress(Run()) == {"route": set_based.ROUTE}


def test_the_set_based_route_is_for_a_whole_run():
	assert write_routes.set_based({"Person": write_routes.SET_BASED, "Case": write_routes.SET_BASED})
	assert not write_routes.set_based({"Person": write_routes.SQL, "Case": write_routes.DOCUMENT})
	assert not write_routes.set_based({})
	with pytest.raises(ValueError):
		write_routes.set_based({"Person": write_routes.SET_BASED, "Case": write_routes.DOCUMENT})


def test_the_document_worker_cannot_be_given_the_set_based_route():
	with pytest.raises(ValueError):
		write_routes.route_for({"Person": write_routes.SET_BASED}, "Person")


def test_business_batches_end_before_assignments_and_shares():
	assert set_based._business_batches(publication(("Person", 1), ("Record Tag", 1), ("ToDo", 1))) == 2
	assert set_based._business_batches(publication(("ToDo", 1))) == 0


@pytest.mark.parametrize(
	("stored", "published"),
	[
		(0.0, "0E-9"),
		(Decimal("12.500000000"), 12.5),
		(1, "1"),
		("0151fa7b-dc1a-701e-8000-00000000001a", "0151fa7b-dc1a-701e-8000-00000000001a"),
		(datetime(2026, 11, 20, 7, 16, 1), datetime(2026, 11, 20, 7, 16, 1)),
		(date(2026, 11, 20), date(2026, 11, 20)),
		(None, None),
	],
)
def test_values_that_are_the_same(stored, published):
	assert set_based._equal(stored, published)


@pytest.mark.parametrize(
	("stored", "published"),
	[
		(None, ""),
		("", None),
		(0.1, "0.2"),
		("Named", "Hidden"),
		("text", 3),
		(date(2026, 11, 20), date(2026, 11, 21)),
	],
)
def test_values_that_differ(stored, published):
	assert not set_based._equal(stored, published)
