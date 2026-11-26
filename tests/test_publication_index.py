import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from ucm_migration_loader import publication_index as index


def test_verified_index_rejects_changed_payload_and_replaced_files(tmp_path):
    path = tmp_path / "publication.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "create table batches(sequence integer primary key, sha text, payload blob)"
        )
        digest = hashlib.sha256(b"approved")
        manifest = {
            "batches": 1,
            "batch_chain_sha256": hashlib.sha256(digest.digest()).hexdigest(),
        }
        db.execute(
            "insert into batches values (1,?,?)", (digest.hexdigest(), b"approved")
        )
        db.commit()
        stamp = json.loads(index.build_index(db, path, manifest))
        index.check_identity(path, stamp)
        with index.index_path(path).open("rb") as hashes:
            assert index.read_batch(db, hashes, 1, 1)[1] == b"approved"
            assert index.read_batch(db, hashes, 2, 1) is None
            db.execute("update batches set payload=?", (b"changed",))
            with pytest.raises(ValueError, match="approved hash"):
                index.read_batch(db, hashes, 1, 1)
        db.commit()
        with pytest.raises(ValueError, match="changed"):
            index.check_identity(path, stamp)
        with pytest.raises(ValueError, match="manifest"):
            index.build_index(db, path, {**manifest, "batches": 2})
        assert index.index_path(path).read_bytes() == digest.digest()
