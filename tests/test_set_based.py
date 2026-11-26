"""The parts of the set-based route that need no site: how batches are grouped into transactions, where the
business batches end, and when a stored value equals a published one."""

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

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


def shape(groups):
	return [(doctype, [sequence for sequence, _sha, _items in group]) for doctype, group in groups]


def test_batches_of_one_doctype_share_a_transaction():
	db = publication(("Person", 3), ("Person", 3), ("Case", 2), ("ToDo", 1), ("Record Share", 1))
	assert shape(set_based._groups(db, 0, None)) == [("Person", [1, 2]), ("Case", [3])]


def test_a_transaction_closes_at_the_row_limit(monkeypatch):
	monkeypatch.setattr(set_based, "COMMIT_ROWS", 5)
	db = publication(("Person", 3), ("Person", 3), ("Person", 3))
	assert shape(set_based._groups(db, 0, None)) == [("Person", [1, 2]), ("Person", [3])]


def test_apply_resumes_after_the_last_committed_batch_and_stops_where_asked():
	db = publication(("Person", 1), ("Person", 1), ("Case", 1), ("Activity", 1))
	assert shape(set_based._groups(db, 1, 3)) == [("Person", [2]), ("Case", [3])]


def test_a_doctype_outside_the_profile_is_refused(monkeypatch):
	monkeypatch.setattr(set_based, "_", str)  # no site here to translate the message for
	db = publication(("Person", 1), ("User", 1))
	with pytest.raises(ValueError):
		list(set_based._groups(db, 0, None))


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
