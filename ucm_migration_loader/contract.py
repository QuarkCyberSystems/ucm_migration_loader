"""What the publishing command and the site must agree on. Shared by the standalone client and Frappe."""

import json
import re
from hashlib import sha256
from uuid import UUID

# the largest batch a sealed publication holds, which is also what a publication is sealed with by default
MAX_PUBLICATION_BATCH_DOCUMENTS = 1000
MAX_PUBLICATION_BATCH_BYTES = 4 * 1024 * 1024
# the pieces a sealed file travels in: the sender cuts them, and the site checks every offset against them
TRANSFER_CHUNK = 512 * 1024


def table_signatures(read_ddl, doctypes, *, sort_indexes=False):
	signatures = {}
	for dt in doctypes:
		ddl = read_ddl(f"show create table `tab{dt}`").replace("\r\n", "\n")
		columns, separator, options = ddl.rpartition("\n)")
		if sort_indexes:
			definitions = columns.splitlines()

			def is_index(line):
				return line.lstrip().startswith(
					(
						"KEY ",
						"UNIQUE KEY ",
						"FULLTEXT KEY ",
						"SPATIAL KEY ",
						"PRIMARY KEY ",
					)
				)

			ordered = [line.rstrip(",") for line in definitions[1:] if not is_index(line)]
			indexes = sorted(line.rstrip(",") for line in definitions[1:] if is_index(line))
			columns = definitions[0] + "\n" + ",\n".join([*ordered, *indexes])
		options = re.sub(r"\bAUTO_INCREMENT=\d+\s*", "", options, count=1)
		signatures[dt] = sha256((columns + separator + options).encode()).hexdigest()
	return signatures


def encoded(value):
	return json.dumps(
		value,
		sort_keys=True,
		separators=(",", ":"),
		ensure_ascii=False,
		allow_nan=False,
	).encode()


def digest(value):
	return sha256(encoded(value)).hexdigest()


def check_code(fingerprint: str) -> str:
	"""A short form of a sealed file's fingerprint, for one person to tell another: the publishing operator
	reads it from their command, and the person who approves types it."""
	return f"{fingerprint[:4]}-{fingerprint[4:8]}".upper()


def check_uuid(value):
	if not isinstance(value, str) or str(UUID(value)) != value:
		raise ValueError("Expected a canonical UUID")
