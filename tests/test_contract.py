"""What the publishing command and the site agree on, without a site: the order of activities and the tables'
fingerprints."""

from uuid import uuid4

import pytest


def test_activity_batches_are_deterministic_and_follow_committed_prerequisites():
	from ucm_migration_loader.activity_order import activity_batches

	opening, request, answer, independent = sorted(str(uuid4()) for _ in range(4))
	rows = [
		(answer, opening, request),
		(request, opening, opening),
		(independent, None, None),
		(opening, None, None),
	]
	expected = [[opening, independent], [request], [answer]]
	assert list(activity_batches(rows, 2)) == expected == list(activity_batches(reversed(rows), 2))
	with pytest.raises(ValueError, match="missing"):
		list(activity_batches(rows[:-1], 2))
	with pytest.raises(ValueError, match="Cyclic"):
		list(activity_batches([(request, answer, None), (answer, request, None)], 1))


def test_service_closure_follows_all_members_even_if_its_key_sorts_first():
	from ucm_migration_loader.activity_order import activity_batches

	closing, opening, action = sorted(str(uuid4()) for _ in range(3))
	rows = [
		(closing, opening, None, True),
		(opening, None, None, False),
		(action, opening, None, False),
	]
	assert list(activity_batches(rows, 25)) == [[opening], [action], [closing]]


def test_schema_ignores_index_declaration_order_only():
	from ucm_migration_loader.contract import table_signatures

	def fingerprint(keys):
		return table_signatures(
			lambda _: (
				"CREATE TABLE `tabT` (\n  `x` int,\n  `y` int,\n" + ",\n".join(keys) + "\n) ENGINE=InnoDB"
			),
			["T"],
			sort_indexes=True,
		)

	first = "  KEY `pair` (`x`,`y`)"
	second = "  UNIQUE KEY `single` (`y`)"
	assert fingerprint([first, second]) == fingerprint([second, first])
	assert fingerprint([first, second]) != fingerprint([first.replace("x`,`y", "y`,`x"), second])
	assert fingerprint([first, second]) != fingerprint([first])
