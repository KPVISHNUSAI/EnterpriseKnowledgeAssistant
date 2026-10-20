# Permission Proof: design

Status: built (2026-10-20). The owner skipped the design questions, so every decision
below marked *(my call)* is the builder's and open to change.

## Why

The buyer is the CISO of a mid-size regulated firm who keeps restricted documents (board
papers, HR cases, M&A) out of Copilot because AI assistants overshare. Telling them "we
enforce permissions" is worth little. Permission Proof turns that claim into evidence they
can re-run on their own documents and permissions, without the vendor: a leak test that
asks the assistant, as every access group in the organization, about every document that
group cannot read, and checks what would have been sent to the model.

The pitch line it supports: *"Run it on your own documents every week, without us."*

## The demo it enables

1. Two people ask the same question: one gets a cited answer, the other gets "the
   documents don't answer this" (exists).
2. Revoke a share: yesterday's answers are hidden (exists, gap 1).
3. **Run leak test** (new): "Passed. No leak found in 312 questions asked as 5 access
   groups." Questions, not model requests, are the headline: a question that finds
   nothing builds no model request, so a group that can read nothing has questions but no
   requests *(my call, after the end-to-end test showed "0 prompts")*.
4. **Simulate a mis-share** (new): pick a person and a workspace they are not in. The
   test treats that person as wrongly given access and must report it: "Caught. The test
   reported 30 findings about 10 documents." A caught simulation is shown as a success
   (the test works) and an uncaught one as a failure *(my call)*. Real permissions are never
   touched.
5. Weekly automatic runs, a history, and a printable report for the model-risk file.

## What it proves, and what it does not

It proves, for the probes it ran: the application's retrieval never returned a passage,
and the assembled model request never contained text, from a document the group may not
read under the documented rule. The rule is evaluated by a second, independent
implementation, not by the code under test.

It does not prove that the permissions themselves are right (oversharing), that other
paths (document pages, search UI, exports) are safe beyond the shared retrieval code, or
anything about model behaviour. The UI says "no leak found in N prompts", never
"leak-proof".

## How a run works

1. **Snapshot.** One read-only `REPEATABLE READ` transaction for the whole probing phase,
   so a permission change mid-run cannot produce a false alarm. It loads active members,
   workspace memberships, document grants, documents and indexed chunks.
2. **Oracle.** A separate implementation of the read rule in `access.py`'s docstring,
   over the snapshot rows: the readable document set of every active member. It never
   calls `access.py`.
3. **Access groups.** Members with the same organization role, workspace roles and
   readable set form one group; one representative (first by email) is asked for the
   group *(my call)*. Org admins and members of no workspace are groups too.
4. **Access check.** For every member, the application's readable set
   (`readable_documents_clause`) must equal the oracle's. Per member, not per group: the
   rule reads per-person records (uploads, personal grants) that two people in one group
   can hold with the same effect today, so a bug in how they are read would show only for
   one of them *(after review)*.
5. **Probes**, generated from the documents, with no model and no setup *(my call)*:
   per indexed document, its title and an excerpt from its first and middle chunk (up to
   3 probes, each at most 300 characters). A group gets the probes of every indexed
   document it cannot read. Queries copied from a document are the strongest way to pull
   that document into retrieval.
6. **Per (group, probe):** the chat's own `retrieve` (configured mode, reranking off,
   since reranking only reorders and filters the same candidates), `build_context`, and
   the model request (`build_messages`) built without the question. No model call. Two
   checks:
   - every retrieved candidate's document is oracle-readable for the group (this covers
     titles and section names, which the prompt carries);
   - the request contains no 50-character window of normalized text that occurs only in
     documents the group cannot read (the evaluation's check, made fast with an index of
     windows taken every 25 characters). The question is left out when the request is
     built, not cut out afterwards: the probe is copied from a document, so cutting it
     would also cut it out of a leaked passage. The fixed system prompt is left out too,
     and titles and section names count as their document's text, so a readable
     document's title quoted inside a restricted one is not a false alarm *(after
     review)*.
7. **Cap.** At most `PERMISSION_PROOF_MAX_PROBES` (default 5000) probe executions per
   run. Above that, a deterministic sample spread over groups and documents; the report
   states the coverage. Nothing is dropped silently.
8. **Verdict.** Passed: no finding, no access mismatch. If no group has an unreadable
   document there is nothing to test, and the report says so instead of passing.

## Simulation (the test button)

The admin picks a person and a workspace they do not belong to that holds indexed
documents the person cannot read. The run tests only that person, whose principal gets
an in-memory viewer membership of the workspace, as if the application wrongly granted
access. The oracle still uses the real records, so the run must report findings: "caught".
Nothing is written to memberships or grants, and no code path in `access.py` changes.
Simulation runs are labelled as such and never become the organization's latest result.

## Who sees what

Org admins only, to run and to read. Reports hold IDs and counts, never document text. A
document title is shown only if the viewer can read the document (the audit log's rule);
otherwise "a document in <workspace>". Group members are listed by name, since admins
manage people.

## Schedule and history

A weekly run per organization (Celery beat; `PERMISSION_PROOF_SCHEDULE`, crontab fields,
default Monday 02:00 UTC, empty disables) *(my call)*. Runs are kept with their reports.
One run at a time per organization; a run queued or running longer than the worker's
time limit counts as interrupted and no longer blocks a new one.

## Interfaces

- Table `permission_proof_runs` (row-level security like every tenant table): kind
  (`manual`, `scheduled`, `simulation`), simulation target, status (`queued`, `running`,
  `completed`, `error`), verdict (`passed`, `failed`, `nothing_to_test`), timestamps,
  requested_by, and JSON `summary`, `groups`, `findings`.
- `POST /api/v1/admin/permission-proof/runs` (optional `simulate: {user_id,
  workspace_id}`), `GET .../runs`, `GET .../runs/{id}`. Org admin only. 409 while a run
  is active; 422 for a simulation target that would test nothing.
- Celery task `eka.proof.run` on the existing `maintenance` queue *(my call: no new queue
  to deploy)*.
- Audit: `permission_proof.run_requested`, `permission_proof.run_completed`, text-free.
- Admin tab "Permission proof": latest result, Run button, simulation dialog, history,
  run detail with groups and findings, a method section, and Print (the browser's
  save-as-PDF) *(my call: no PDF library)*.

## Tests that must exist

- The oracle equals `readable_documents_clause` on many seeded random permission graphs.
- A run on test data passes; a simulation is caught; a deliberately broken access clause
  (an admin bypass patched back in) makes a normal run fail. The check has to be able to
  fail.
- The window index finds copied text and ignores text shared with readable documents.
- API: admin-only, 409, 422, no document text anywhere in a report, title visibility.
- Frontend component tests, and an end-to-end test: run passes on the demo data and a
  simulation is caught.

## Not in this version

Custom questions; the follow-up rewrite path; oversharing rules ("these documents may only
be read by X"), which would catch real mis-shares rather than simulated ones; generated
PDFs; notifications on failure.

## Anti-goals

The test never widens anyone's real access, never stores or shows document text, and
never claims more than it checked.
