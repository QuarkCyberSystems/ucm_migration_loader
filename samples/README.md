# Sample files of a publishing host

The three files `ucm-push` reads on the host that seals and sends delegations. `ucm-push init` writes the same
three files into a folder, private from the start, for you to fill in. Nothing here is a real address, account or
password.

| File | What it is | Named by |
|---|---|---|
| [ucm-push.toml](ucm-push.toml) | The host's settings: what is the same for every delegation | `--config`, `UCM_PUSH_CONFIG`, or the folder you work from |
| [source.json](source.json) | The connection to the publishing database | `source_config` in the settings |
| [quality-db.json](quality-db.json) | The connection to the data quality database. Optional | `quality_config` in the settings |

A fourth file holds no structure: the token file named by `token_file`, one line with the API key and secret of
your publishing account on the UCM site, as `key:secret`.

Keep all four outside any repository, readable by your account only (`chmod 600`). `ucm-push` refuses a token
file that other accounts of the host can read.

## source.json and quality-db.json

Both are the arguments of one database connection, as JSON. JSON takes no comments, so the keys are explained
here.

| Key | Meaning | In the sample |
|---|---|---|
| `host`, `port` | Where the MariaDB server listens | `publisher-db.example.org`, `3306` |
| `user`, `password` | The account that connects | `migration_reader` and a placeholder |
| `database` | The database (schema) to read or write | `ucm_publication` |
| `unix_socket` | For a server on the same host: the path of its socket, in place of `host` and `port` | not shown |
| `ssl_ca` | The certificate of the authority that signed the server's certificate, to connect over TLS | not shown |
| `ssl_verify_cert` | `true` to refuse a server whose certificate does not verify | not shown |
| `connect_timeout`, `read_timeout` | Seconds to wait for the connection and for an answer | not shown |

A connection to a local server through its socket:

```json
{
	"unix_socket": "/run/mysqld/mysqld.sock",
	"user": "migration_reader",
	"password": "<password of the read-only account>",
	"database": "ucm_publication"
}
```

A connection over TLS:

```json
{
	"host": "publisher-db.example.org",
	"port": 3306,
	"user": "migration_reader",
	"password": "<password of the read-only account>",
	"database": "ucm_publication",
	"ssl_ca": "/srv/migration/credentials/publisher-db-ca.pem",
	"ssl_verify_cert": true
}
```

**What each account needs.**

- The account of `source.json` reads and never writes: `SELECT` on the publishing database. `ucm-push` opens one
  read-only view of the database for a whole sealing.
- The account of `quality-db.json` writes one table, `ucm_target_metric` unless `quality_table` names another:
  `SELECT`, `INSERT` and `DELETE` on it. A run's rows replace the rows the same run left before. The account
  needs `CREATE` only if you pass `--create-quality-table`; the table's definition is in the repository's README.

## ucm-push.toml

Each setting is explained in the file. The paths in the sample assume the host keeps its migration files under
`/srv/migration`; `ucm-push init FOLDER` writes them for the folder you name.

| Setting | Needed | Meaning |
|---|---|---|
| `site` | yes | The receiving site, as its bench names it |
| `url` | yes | The site's HTTPS address |
| `source_config` | yes | The path of `source.json` |
| `publications` | yes | Where a folder per snapshot is made |
| `token_file` | yes, unless `UCM_MIGRATION_TOKEN` is set | The path of the token file |
| `private_files` | when the publication has pictures | The private files folder of the publishing site |
| `quality_config`, `quality_table` | no | The path of `quality-db.json`, and the metrics table |
| `ca_bundle` | no | The certificate of a private authority that signed the site's certificate |
| `batch_size`, `batch_bytes` | no | Records and bytes per batch: 1000 and 4194304 by default |
