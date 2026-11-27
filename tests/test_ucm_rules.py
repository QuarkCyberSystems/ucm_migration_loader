"""How UCM's save rules are read and compared, without a site and without UCM: what a fingerprint follows and
what it ignores, which called functions are found, and how a difference is reported."""

import sys

from ucm_migration_loader.ucm_migration_loader import ucm_rules


def check(doc):
	"""A rule."""
	if not doc.name:  # a comment
		raise ValueError("no name")


def check_reworded(doc):
	"""The same rule, told otherwise."""

	# another comment
	if not doc.name:
		raise ValueError("no name")


def check_changed(doc):
	"""A rule."""
	if not doc.name or doc.name == "x":
		raise ValueError("no name")


def helper(doc):
	return doc


class Base:
	def validate(self):
		check(self)

	def own_rule(self):
		return helper(self)


class Record(Base):
	def validate(self):
		super().validate()
		self.own_rule()
		later(self)
		len(self.name)

	def before_insert(self):
		from tests_sample import imported

		imported(self)


def later(doc):
	return doc


def test_a_fingerprint_ignores_comments_docstrings_and_layout(monkeypatch):
	monkeypatch.setattr(
		ucm_rules.inspect,
		"getsource",
		lambda f, real=ucm_rules.inspect.getsource: real(f).replace(f.__name__, "rule", 1),
	)
	assert ucm_rules.fingerprint(check) == ucm_rules.fingerprint(check_reworded)
	assert ucm_rules.fingerprint(check) != ucm_rules.fingerprint(check_changed)


def test_the_functions_a_hook_calls_are_found(monkeypatch):
	module = sys.modules[__name__]
	sample = type(sys)("tests_sample")
	sample.imported = helper
	monkeypatch.setitem(sys.modules, "tests_sample", sample)
	monkeypatch.setattr(ucm_rules, "APP", __name__.split(".")[0])
	found = ucm_rules.called(Record.validate, Record)
	# a method of the class, a function of the module; not Python's own, and not super()'s
	assert set(found) == {f"{module.__name__}.Base.own_rule", f"{module.__name__}.later"}


def test_a_function_imported_in_the_body_is_found(monkeypatch):
	sample = type(sys)("tests_sample")
	sample.imported = helper
	monkeypatch.setitem(sys.modules, "tests_sample", sample)
	monkeypatch.setattr(ucm_rules, "APP", "tests_sample")
	monkeypatch.setattr(helper, "__module__", "tests_sample")
	assert list(ucm_rules.called(Record.before_insert, Record)) == ["tests_sample.helper"]


def test_differences_name_what_is_new_gone_and_changed():
	kept = {"Person": {"a.validate": "1", "a.old": "2"}, "Case": {"c.validate": "3"}}
	now = {"Person": {"a.validate": "9", "a.added": "4"}, "Case": {"c.validate": "3"}, "Note": {"n.v": "5"}}
	assert ucm_rules.differences(now, kept) == [
		"Note: n.v is new",
		"Person: a.added is new",
		"Person: a.old is gone",
		"Person: a.validate changed",
	]
	assert ucm_rules.differences(kept, kept) == []
