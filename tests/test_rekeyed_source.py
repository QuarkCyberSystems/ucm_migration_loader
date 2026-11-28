"""The re-keyed view of a generator source: which values move to the new key space, and that a parameter sent
back to the source is the key the source stores."""

from rekeyed_source import Rekeyed

PERSON = "0151fa7b-dc00-7001-8000-000000000000"
PLACE = "0151fa7b-dc00-702c-8000-000000000000"
OWN_ACTIVITY_TYPE = "0151fa7b-dcab-7024-8000-0000000000ab"
SHARED_ACTIVITY_TYPE = "0151fa7b-dc01-7024-8000-000000000001"
PLAN = {
	"digit": "2",
	"codes": ["01", "2d"],
	"keys": {OWN_ACTIVITY_TYPE: "0151fa7b-dcab-7224-8000-0000000000ab", "source-tag": "target-tag"},
	"values": {"BOL:LOCAL": "W50BOL:LOCAL"},
}


class Cursor:
	"""A cursor that returns the rows it was given and keeps the parameters it was sent."""

	def __init__(self, rows):
		self.rows, self.sent = list(rows), None

	def execute(self, query, args=None):
		self.sent = args

	def fetchone(self):
		return self.rows.pop(0) if self.rows else None

	def fetchall(self):
		rows, self.rows = self.rows, []
		return rows

	def __iter__(self):
		return iter(self.fetchall())


class Connection:
	def __init__(self, rows=()):
		self.made = Cursor(rows)

	def cursor(self, *args, **kwargs):
		return self.made


def view(rows=()):
	connection = Connection(rows)
	return Rekeyed(connection, PLAN), connection.made


def test_a_key_the_delegation_owns_moves_and_a_shared_one_stays():
	source, _cursor = view()
	assert source.out(PERSON) == "0151fa7b-dc00-7201-8000-000000000000"
	assert source.out(PLACE) == PLACE
	assert source.out(SHARED_ACTIVITY_TYPE) == SHARED_ACTIVITY_TYPE


def test_an_owned_row_of_a_catalog_that_also_holds_shared_rows_moves_by_its_key():
	source, _cursor = view()
	assert source.out(OWN_ACTIVITY_TYPE) == "0151fa7b-dcab-7224-8000-0000000000ab"
	assert source.into("0151fa7b-dcab-7224-8000-0000000000ab") == OWN_ACTIVITY_TYPE


def test_a_key_inside_another_value_moves_with_it():
	source, _cursor = view()
	assert source.out("pic-" + PERSON) == "pic-0151fa7b-dc00-7201-8000-000000000000"
	assert source.out(f'["{PERSON}", "{PLACE}"]') == f'["0151fa7b-dc00-7201-8000-000000000000", "{PLACE}"]'


def test_task_names_tags_and_unique_codes_are_replaced():
	source, _cursor = view()
	assert source.out("bxt0000012") == "bxw0000012"
	assert source.out("source-tag") == "target-tag"
	assert source.out("BOL:LOCAL") == "W50BOL:LOCAL"
	assert source.out("Open") == "Open"
	assert source.out(12) == 12
	assert source.out(None) is None


def test_a_renamed_code_inside_a_list_of_tag_codes_is_renamed_and_the_list_kept_in_order():
	source, _cursor = view()
	assert source.out("BOL:LOCAL,SENS") == "SENS,W50BOL:LOCAL"
	assert source.out("HSENS,SENS") == "HSENS,SENS"
	assert source.out("One, two") == "One, two"


def test_every_moved_value_is_taken_back_for_a_query():
	source, _cursor = view()
	for value in (PERSON, PLACE, OWN_ACTIVITY_TYPE, "pic-" + PERSON, "bxt0000012", "source-tag", "BOL:LOCAL"):
		assert source.into(source.out(value)) == value


def test_parameters_reach_the_source_under_its_own_keys():
	source, cursor = view()
	moved = source.out(PERSON)
	source.cursor().execute("select 1", (moved, [moved, PLACE], {"name": moved}))
	assert cursor.sent == (PERSON, [PERSON, PLACE], {"name": PERSON})
	source.cursor().execute("select 1")
	assert cursor.sent is None


def test_rows_come_back_under_the_new_keys_whatever_their_shape():
	source, _cursor = view([(PERSON, 1), {"name": PERSON, "place": PLACE}, (PERSON,), (PLACE,)])
	cursor = source.cursor()
	assert cursor.fetchone() == ("0151fa7b-dc00-7201-8000-000000000000", 1)
	assert cursor.fetchone() == {"name": "0151fa7b-dc00-7201-8000-000000000000", "place": PLACE}
	assert list(cursor) == [("0151fa7b-dc00-7201-8000-000000000000",), (PLACE,)]
	assert cursor.fetchone() is None
