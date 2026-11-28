"""Every module of the app loads when it is the first one asked for. A request, a job and a bench command each
start from a different module, so two modules that need each other at load fail for some of them only."""

import subprocess
import sys
from pathlib import Path

from frappe.tests import IntegrationTestCase

import ucm_migration_loader

# one interpreter that knows Frappe, and a fresh copy of it for each module
FIRST = """
import importlib, os, sys
import frappe
failed = 0
for name in sys.argv[1:]:
	child = os.fork()
	if not child:
		try:
			importlib.import_module(name)
		except Exception as refused:
			sys.stderr.write(f"{name}: {type(refused).__name__}: {refused}\\n")
			os._exit(1)
		os._exit(0)
	failed += bool(os.waitpid(child, 0)[1])
sys.exit(1 if failed else 0)
"""


class TestImports(IntegrationTestCase):
	def test_every_module_loads_first(self):
		root = Path(ucm_migration_loader.__file__).parent
		modules = sorted(
			".".join(("ucm_migration_loader", *path.relative_to(root).with_suffix("").parts)).removesuffix(
				".__init__"
			)
			for path in root.rglob("*.py")
			if "tests" not in path.parts and not path.name.startswith("test_")
		)
		self.assertGreater(len(modules), 30)
		done = subprocess.run(
			[sys.executable, "-c", FIRST, *modules], capture_output=True, text=True, timeout=300, check=False
		)
		self.assertEqual(done.returncode, 0, done.stderr[-2000:])
