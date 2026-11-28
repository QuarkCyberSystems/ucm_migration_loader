"""What a save runs in UCM for the tables the set-based route writes with SQL, and the reading of it the route
was last reviewed against.

No controller runs for a row the set-based route copies, so every rule a save applies is named in `set_based`
(`integrity`, `_record_rules`, `_derive`) or left to the documents the verification samples. That is a reading of
UCM as it was when the route was written. This module reads UCM as it is: for each of those DocTypes, the hooks of
its controller that a save runs, in every UCM class it inherits from, and the UCM functions those hooks call. Each
gets a fingerprint of its code, comments and docstrings aside.

`ucm_rules_reviewed.json` keeps the fingerprints of the UCM the route was last reviewed against. The app's tests
fail when UCM's differ (tests/test_ucm_rules.py), naming what changed: read the change, bring `set_based` in line
where the rule is one it copies, and keep the new reading:

	bench --site <developer site> execute ucm_migration_loader.ucm_migration_loader.ucm_rules.review

A fingerprint follows a hook and what it calls directly, not what those functions call in turn: the rules the route
copies in SQL are compared with the documents themselves by the same tests.
"""

import ast
import hashlib
import importlib
import inspect
import json
import textwrap
from pathlib import Path

# what Frappe calls on a controller when a record is inserted or saved
HOOKS = (
	"before_naming",
	"autoname",
	"before_validate",
	"validate",
	"before_insert",
	"before_save",
	"after_insert",
	"on_update",
	"on_change",
)
APP = "ucm"
REVIEWED = Path(__file__).with_name("ucm_rules_reviewed.json")


def written_doctypes() -> list[str]:
	"""The DocTypes whose rows the set-based route writes with SQL: the publication's, without those it leaves to
	the documents, and with their child tables."""
	from ucm_migration_loader.frozen import CHILDREN, ORDER

	from .set_based import DEFERRED, THROUGH_DOCUMENTS

	parents = [doctype for doctype in ORDER if doctype not in THROUGH_DOCUMENTS and doctype not in DEFERRED]
	return parents + [child for parent in parents for child in CHILDREN.get(parent, ())]


def fingerprint(function) -> str:
	"""A digest of a function's code that a comment, a docstring or the layout does not change."""
	tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
	for node in ast.walk(tree):
		body = getattr(node, "body", None)
		if (
			isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
			and body
			and isinstance(body[0], ast.Expr)
			and isinstance(body[0].value, ast.Constant)
			and isinstance(body[0].value.value, str)
		):
			node.body = body[1:] or [ast.Pass()]
	return hashlib.sha256(ast.dump(tree).encode()).hexdigest()[:16]


def _of_app(found) -> bool:
	return inspect.isfunction(found) and (found.__module__ or "").split(".")[0] == APP


def called(function, owner: type | None = None) -> dict:
	"""The functions of UCM a function calls directly, by their qualified name: a name of its module, a method of
	its own class, a function of a module it names, and what it imports in its body."""
	tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
	scope = dict(function.__globals__)
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == APP:
			module = importlib.import_module(node.module)
			scope.update(
				{alias.asname or alias.name: getattr(module, alias.name, None) for alias in node.names}
			)
	out = {}
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		target, found = node.func, None
		if isinstance(target, ast.Name):
			found = scope.get(target.id)
		elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
			if target.value.id == "self" and owner is not None:
				found = inspect.getattr_static(owner, target.attr, None)
			elif inspect.ismodule(scope.get(target.value.id)):
				found = getattr(scope[target.value.id], target.attr, None)
		if _of_app(found):
			out[f"{found.__module__}.{found.__qualname__}"] = found
	return out


def controller(doctype: str) -> type | None:
	"""The controller class of a UCM DocType, read from the app's layout: no site is needed."""
	name = doctype.lower().replace(" ", "_")
	try:
		module = importlib.import_module(f"{APP}.{APP}.doctype.{name}.{name}")
	except ImportError:
		return None  # a DocType of Frappe (File)
	return getattr(module, doctype.replace(" ", ""), None)


def rules(doctype: str) -> dict[str, str]:
	"""{name: fingerprint} of what a save of this DocType runs in UCM: the hooks its controller defines or
	inherits from a UCM class, and the UCM functions they call."""
	cls = controller(doctype)
	if cls is None:
		return {}
	found = {}
	for base in cls.__mro__:
		if (base.__module__ or "").split(".")[0] != APP:
			continue
		for hook in HOOKS:
			method = base.__dict__.get(hook)
			if not inspect.isfunction(method):
				continue
			found[f"{base.__module__}.{base.__qualname__}.{hook}"] = method
			found.update(called(method, cls))
	return {name: fingerprint(function) for name, function in sorted(found.items())}


def fingerprints() -> dict[str, dict[str, str]]:
	"""The rules of every DocType the set-based route writes, as UCM has them now."""
	return {doctype: rules(doctype) for doctype in written_doctypes()}


def reviewed() -> dict:
	return json.loads(REVIEWED.read_text())


def differences(current: dict, kept: dict) -> list[str]:
	"""What differs between UCM's rules now and the reviewed ones, one line each."""
	out = []
	for doctype in sorted(set(current) | set(kept)):
		now, then = current.get(doctype, {}), kept.get(doctype, {})
		for name in sorted(set(now) | set(then)):
			if name not in then:
				out.append(f"{doctype}: {name} is new")
			elif name not in now:
				out.append(f"{doctype}: {name} is gone")
			elif now[name] != then[name]:
				out.append(f"{doctype}: {name} changed")
	return out


def version() -> str:
	"""The UCM read: its version, and its commit where the app is a git checkout."""
	import subprocess

	module = importlib.import_module(APP)
	commit = subprocess.run(
		["git", "-C", str(Path(module.__file__).parent), "rev-parse", "--short", "HEAD"],
		capture_output=True,
		text=True,
		check=False,
	).stdout.strip()
	return f"{getattr(module, '__version__', '')} {commit}".strip()


def review() -> dict:
	"""Keep UCM's rules as they are now as the reviewed ones. For a developer, after reading what changed."""
	import frappe

	if not frappe.conf.developer_mode:
		frappe.throw(frappe._("The reviewed rules are kept from a developer site"))
	changed = differences(fingerprints(), reviewed()["rules"]) if REVIEWED.exists() else []
	REVIEWED.write_text(
		json.dumps({"ucm": version(), "rules": fingerprints()}, indent=1, sort_keys=True) + "\n"
	)
	return {"ucm": version(), "kept": str(REVIEWED), "changed_since_the_last_review": changed}
