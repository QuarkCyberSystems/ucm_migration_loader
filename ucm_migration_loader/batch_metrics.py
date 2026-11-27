"""Time selected worker phases within a single batch, including nested call sites."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import monotonic

_active = ContextVar("ucm_migration_metrics", default=None)


@contextmanager
def collect():
	totals = Counter()
	token = _active.set(totals)
	try:
		yield totals
	finally:
		_active.reset(token)


def measure(phase):
	def decorate(func):
		@wraps(func)
		def timed(*args, **kwargs):
			totals = _active.get()
			if totals is None:
				return func(*args, **kwargs)
			started = monotonic()
			try:
				return func(*args, **kwargs)
			finally:
				totals[phase] += monotonic() - started

		return timed

	return decorate
