"""Order activities after their opening service and answered activity in the same frozen wave."""

import hashlib
import sqlite3
from contextlib import closing

import pymysql

from .contract import MAX_PUBLICATION_BATCH_DOCUMENTS, check_uuid, encoded

DEPENDENCIES = """select a.name, a.activity_service_tech_id, a.activity_associated_tech_id,
    t.activity_type_ucm_category='End of service' from tabActivity a
    left join `tabActivity Type` t on t.name=a.activity_type_tech_id where a.context_tech_id=%s"""


def activity_batches(rows, size):
	if not 1 <= size <= MAX_PUBLICATION_BATCH_DOCUMENTS:
		raise ValueError("Invalid activity batch size")
	# A private temporary SQLite database keeps the graph off the Python heap.
	# MariaDB stays read-only. Closing the connection releases its temporary file.
	with closing(sqlite3.connect("")) as graph:
		graph.executescript("""
            create table nodes (id text primary key, pending integer not null);
            create table edges (prerequisite text, dependent text, primary key(prerequisite, dependent)) without rowid;
            create table services (activity text primary key, opening text, closing integer);
            create index service_members on services(opening);
            create index available on nodes(pending, id);
            create temp table selected (id text primary key);
        """)
		count = 0
		for name, service, answered, *end_flag in rows:
			if service:
				graph.execute(
					"insert into services values (?,?,?)",
					(name, service, bool(end_flag and end_flag[0])),
				)
			parents = {ref for ref in (service, answered) if ref is not None}
			for ref in (name, *parents):
				check_uuid(ref)
			graph.execute("insert into nodes values (?,?)", (name, len(parents)))
			graph.executemany("insert into edges values (?,?)", ((p, name) for p in parents))
			count += 1
		graph.execute("""insert or ignore into edges
            select m.activity, e.activity from services e join services m on m.opening=e.opening
            where e.closing=1 and e.activity<>m.activity""")
		graph.execute("create index incoming on edges(dependent)")
		graph.execute("update nodes set pending=(select count(*) from edges where dependent=nodes.id)")
		if graph.execute(
			"select 1 from edges e left join nodes n on n.id=e.prerequisite where n.id is null limit 1"
		).fetchone():
			raise ValueError("An activity prerequisite is missing from its delegation")
		while count:
			batch = [
				row[0]
				for row in graph.execute("select id from nodes where pending=0 order by id limit ?", (size,))
			]
			if not batch:
				raise ValueError("Cyclic activity service or response links")
			graph.executemany("insert into selected values (?)", ((key,) for key in batch))
			graph.execute("""
                update nodes set pending=pending-next.n from (
                    select e.dependent, count(*) n from selected s cross join edges e on e.prerequisite=s.id
                    group by e.dependent
                ) as next where nodes.id=next.dependent
            """)
			graph.execute("delete from nodes where id in (select id from selected)")
			graph.execute("delete from selected")
			count -= len(batch)
			yield batch


def order_report(connection, context, size):
	check_uuid(context)
	h = hashlib.sha256()
	count = batches = 0
	with connection.cursor(pymysql.cursors.SSCursor) as cur:
		cur.execute(
			DEPENDENCIES,
			context,
		)
		for batch in activity_batches(cur, size):
			count += len(batch)
			batches += 1
			h.update(encoded(batch) + b"\n")
	return {
		"activities": count,
		"batches": batches,
		"batch_size": size,
		"order_sha256": h.hexdigest(),
	}
