"""The names the loader's parts share: its DocTypes, its queue, its roles, the states of a run and the write
routes. Nothing is imported here: the publishing command reads these too, on a host without Frappe."""

RUN = "UCM Migration Run"
BATCH = "UCM Migration Batch"
HELD = "UCM Migration Held Share"
QUEUE = "ucm_migration"

# who runs a migration on the site, and which publishing accounts may propose their hand-offs
ROLE = "Migration Admin"
PUBLISHER = "Migration Publisher"

# the states of a run, in the order a run passes through them
PROPOSED = "Proposed"
AWAITING = "Awaiting publication"
STAGED = "Publication staged"
APPLYING = "Publication applying"
PAUSED = "Publication paused"
READY = "Publication ready"
CLEANING = "Publication cleaning"
ACTIVATED = "Activated"
DISCARDED = "Discarded"
REJECTED = "Rejected"
# a run in one of these has a staged file, and may hold rows of its delegation on the site, hidden
HIDDEN = (STAGED, APPLYING, PAUSED, READY, CLEANING)
# a run in one of these holds neither a file nor a row yet
WAITING = (PROPOSED, AWAITING)

# the write routes of a run's plan
DOCUMENT = "Document API"
SQL = "Validated bulk SQL"
# writes, derives and verifies a delegation in sets: a route for the whole run, never for some DocTypes of it
SET_BASED = "Set-based SQL"
ROUTES = (DOCUMENT, SQL, SET_BASED)
# what a run's route is called where its plan gives several
BY_DOCTYPE = "By DocType"

# the access rows of a delegation, held back until it is activated
DEFERRED = ("ToDo", "Record Share")
