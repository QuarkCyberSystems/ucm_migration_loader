# Limits and edge cases

What the loader refuses, holds back or leaves out, and when you learn of it. A refusal at sealing costs nothing; one at staging comes after the file was sent; one at loading comes after it was staged.

## Fields and tables of a delegation's own

| Case | What happens | When |
|---|---|---|
| The publishing database has a column the site lacks, or the site has one the publication lacks: a custom field, a sensitive field added as a column of Sensitive Information, another UCM release | Refused: *tables differ from this site*, with the DocTypes. The tables of the publishing database and of the site must be the same, column for column and index for index. A delegation's own fields therefore have to be columns of the site, and of every publishing database, before its file is sealed | Staging |
| A field is added to the site, or UCM is updated, while a run is staged or loading | The run stops: the site is no longer the one the file was reviewed for. Discard it and seal again. Keep the site's tables and its UCM release as they are from sealing to activation | The next step that writes |
| The definition of a custom field (its label, type, options, where it shows) | Not carried. Make the field on the site first, as on the publishing side | Staging, as above |
| The sensitive fields and their clearances differ between the publishing database and the site, for any record type, or the delegation's exemptions differ | Refused in a sentence. The whole assignment of sensitive fields is compared, not only the fields this delegation fills | Loading, before the first batch |
| A list of values the delegation uses differs on the site: a term is not there, is not usable, or is not in the list its context uses | Refused with the DocType, or reported as a finding with the field | Loading or verification |
| A table the publication does not carry holds rows of the delegation | Refused, with the table and the count, where the table has a context column or is a file or a task attached to a record. A table of a delegation's own that has neither is not seen, and is left out without a word | Sealing |

## Records that point outside their delegation

| Case | What happens | When |
|---|---|---|
| A share to a delegation that is live on the site | Released at activation, after UCM has checked consent and eligibility again | Activation |
| A share to a delegation that is not live yet | Held as a receipt, and released when that delegation is activated. It is left for review, with the reason, where the owning delegation is no longer active, the share is no longer eligible, or a live share exists by then. A share to a delegation that is never migrated stays held | Activation of either |
| A share that was closed, declined or revoked before | Loaded as history | Activation |
| An open share on a merged person, or one UCM's consent rules no longer allow | The verification stops, with the batch and the item | Verification |
| A record that links to a person, a case or an activity of another delegation, as UCM allows along an accepted share | Refused while that record is not on the site: load the other delegation first. Once it is, the verification still reports *belongs to another context*: the share that would allow the link is released only at activation. Two delegations whose records point at each other cannot both be loaded this way. Known from the code, not yet seen in a rehearsal | Loading, or verification |
| A user named by a record: who created or changed it, who a task is for, who a share is for | Must be on the site under the same e-mail address and full name. A user renamed since counts as missing | Loading, before the first batch |

## What a publication does not carry

- **Files.** A person's picture, as one private file, and nothing else. Other files on a person, and any file on another record, refuse the sealing.
- **What Frappe keeps beside a record:** comments, versions, likes, tags and the timeline. A row whose own framework columns (`_assign`, `_comments`, `_liked_by`, `_user_tags`) are filled is refused at loading.
- **Setup:** teams, security profiles, users' tag clearances, a context's exemptions and entry forms, catalogs and dictionaries. They are expected on the site ([Prepare the receiving context](reference.md#what-the-site-must-hold-before-a-load)).
- **Documents that are submitted or cancelled.** Every row is a draft in Frappe's sense (`docstatus` 0); another is refused at loading.
- **A second load of the same delegation.** A delegation is loaded once, into a context that is locked, hidden and empty; record IDs that are on the site already refuse the staging. Corrections after the activation are made in UCM.

## Size and time

- **One record larger than a batch** (4 MiB, with its child rows) refuses the sealing.
- **The Document API route** is for small delegations: it writes and rechecks record by record, and its activation reads every record again in one transaction (see [Set-based SQL route](reference.md#set-based-sql-route) for the measured times).
- **Assignments and shares** are released in one transaction at activation on every route: about three minutes for 11,000 shares.
- **The set-based route** writes every column the publication carries. Where the site's table still has a column that its UCM no longer defines, this route fills it, and the Document API route refuses it.
- **One sealed file per delegation**, held once on the site until its run is activated or discarded. An approval is refused where the disk cannot hold it.
- **One worker** takes one job at a time: two delegations that are loading take turns.
