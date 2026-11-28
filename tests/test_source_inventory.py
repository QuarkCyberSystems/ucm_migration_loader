"""What the sealing says of a delegation whose persons have files a publication cannot carry."""

import pytest

from ucm_migration_loader import frozen, source_inventory
from ucm_migration_loader.source_inventory import files_refusal

CONTEXT = "01a1f564-23b4-75bd-8d0e-f6ef67851fd4"
FIRST = "0151fa7b-ddc0-7210-8000-0000000001c0"


def test_a_delegation_whose_persons_have_their_pictures_only_is_not_refused():
	assert files_refusal({}) is None


def test_other_attachments_are_refused_with_their_count_and_the_first_person():
	said = files_refusal({"other_attachments": {"files": 1700, "persons": 12, "first_person": FIRST}})
	assert said.startswith(
		f"1,700 files are attached to 12 persons beside the person's picture (first person: {FIRST}). "
	)
	assert said.endswith("correct this in the publishing database, then seal again")
	one = files_refusal({"other_attachments": {"files": 1, "persons": 1, "first_person": FIRST}})
	assert one.startswith("1 file is attached to 1 person beside the person's picture")


def test_a_picture_without_its_one_private_file_is_refused():
	said = files_refusal({"pictures_without_one_private_file": {"persons": 3, "first_person": FIRST}})
	assert said.startswith(
		f"The picture of 3 persons is not held as one private file (first person: {FIRST}). "
	)


def test_both_findings_are_said_together():
	said = files_refusal(
		{
			"other_attachments": {"files": 2, "persons": 2, "first_person": FIRST},
			"pictures_without_one_private_file": {"persons": 1, "first_person": FIRST},
		}
	)
	assert "beside the person's picture" in said and "; the picture of 1 person is not held" in said


class Source:
	"""A publishing database that knows the delegation and finds no activity without a place."""

	def __enter__(self):
		return self

	def __exit__(self, *_exc):
		pass

	def cursor(self):
		return self

	def execute(self, sql, values=None):
		pass

	def fetchone(self):
		return (0,)


def test_the_sealing_refuses_before_it_writes_a_file(monkeypatch, tmp_path):
	found = {"other_attachments": {"files": 2, "persons": 1, "first_person": FIRST}}
	monkeypatch.setattr(
		source_inventory,
		"inventory",
		lambda source, context: {"unsupported_business_rows": {}, "unsupported_person_files": found},
	)
	output = tmp_path / "delegation.sqlite"
	with pytest.raises(ValueError, match="2 files are attached to 1 person beside the person's picture"):
		frozen.freeze(Source(), output, None, "target.example", "DEMO01-1", CONTEXT, 1000, 4 * 1024 * 1024)
	assert not output.exists()
