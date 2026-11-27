"""Receiver-owned manifest checkpoints for a sealed, private SQLite publication."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def identity(path):
	st = path.stat()
	return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def index_path(path):
	return path.with_suffix(".batch-hashes")


def build_index(db, path, manifest):
	"""Called inside the verified publication snapshot, with the receiving run locked."""
	destination = index_path(path)
	chain = hashlib.sha256()
	descriptor, name = tempfile.mkstemp(dir=path.parent)
	os.close(descriptor)
	tmp = Path(name)
	try:
		with tmp.open("wb") as output:
			count = 0
			for seq, value in db.execute("select sequence,sha from batches order by sequence"):
				raw = bytes.fromhex(value)
				count += 1
				if len(raw) != 32 or seq != count:
					raise ValueError("Invalid approved batch sequence")
				output.write(raw)
				chain.update(raw)
			if count != manifest["batches"] or chain.hexdigest() != manifest["batch_chain_sha256"]:
				raise ValueError("Batch hash index differs from approved manifest")
			output.flush()
			os.fsync(output.fileno())
		os.replace(tmp, destination)
		return json.dumps([identity(path), identity(destination)])
	finally:
		tmp.unlink(missing_ok=True)


def check_identity(path, stamp):
	if [identity(path), identity(index_path(path))] != stamp:
		raise ValueError("Sealed publication or its verified index changed; review the staged file")


def read_batch(db, hashes, sequence, total):
	if sequence > total:
		return None
	row = db.execute("select sha,payload from batches where sequence=?", (sequence,)).fetchone()
	hashes.seek((sequence - 1) * 32)
	expected = hashes.read(32)
	if (
		not row
		or len(expected) != 32
		or row[0] != expected.hex()
		or hashlib.sha256(row[1]).digest() != expected
	):
		raise ValueError(f"Publication batch {sequence} differs from the approved hash")
	return row
