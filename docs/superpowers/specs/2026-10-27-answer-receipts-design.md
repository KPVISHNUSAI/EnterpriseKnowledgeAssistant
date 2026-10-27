# Answer receipts: design

Status: built (2026-10-27); see [ARCHITECTURE.md](../../ARCHITECTURE.md#answer-receipts).
The owner asked to continue without design questions, so every decision marked
*(my call)* is the builder's and open to change.

## Why

A compliance officer who acts on an answer needs to show later what it rested on: which
document, which version of it, and whether the answer actually says what that version
says. Today an answer cites passages, but nothing records the version, and nothing checks
a statement against the passage it cites beyond the citation id and quoted text. When a
policy is replaced, an old answer keeps reading as current.

The pitch: every answer comes with a receipt. Each statement is checked against the
passage it cites, by code rather than by another model, and the answer is pinned to the
exact document versions it used. When a cited document is replaced, the old answer says
so.

## What a receipt holds

Computed once, when the answer is produced, and stored with the message:

- **Statements.** The answer split into claims (sentences and list items) with the
  sources each one cites. The check is the evaluation's faithfulness check, moved into
  the application (`app/rag/claims.py`) so the product and the evaluation run the same
  code *(my call)*. Each statement gets one verdict:
  - **supported** ("Matches S1"): every number in it appears in the cited passages, and
    at least half of its new content words do (all its words, when it only restates the
    question). The evaluation's rule skips what comes from the question; receipts must
    not, or a leading question would make its own echo a match (review finding);
  - **unsupported**, with the reason: the numbers missing from the cited passages
    ("“3” is not in S1"), or too few of its words there;
  - **uncited** ("Cites no source").
- **Versions.** For each source, the document version whose text the answer used.
  This is the version that was indexed when the question was asked, not the document's
  latest upload: a new file stays unsearchable until it has been indexed.

Statements are checked only for answers that make claims: answered, partial and
unverified. Abstentions, refusals and errors have none.

## What people see

- Under each answer, a one-line summary: "4 of 4 statements match their sources", or
  "3 of 4 statements match their sources". It opens the full list, with the verdict and
  reason for each statement and the document versions used.
- When a cited document has a newer indexed version than the one the answer used, a
  notice above the answer: "This answer used version 1 of “Travel Policy”. Version 2
  replaced it on <date>." With several replacements, it names the current version and
  when that version went live *(my call)*, one line per document. Only cited documents
  count *(my call)*:
  a passage that was retrieved but not cited did not shape the answer.
- The wording never says "true" or "verified": the check is lexical, so it says "matches"
  and "is not in" *(my call)*.

The receipt does not change the answer's status, which still follows from its citations
*(my call)*.

## Who sees what

A receipt follows the answer's existing redaction. Its statements quote the answer, so
they are withheld when the answer is withheld and erased with it when a document the
answer drew on is deleted. Change notices are computed when history is read, only for documents the reader
can read now.

The audit event for an answer (`chat.answered`) also records the version of each
document it retrieved and the number of statements checked and matched. These are
counts and ids only, never text *(my call)*.

## Evaluation

The receipt check is the evaluation's faithfulness metric, so the existing reports
already measure it: for llama3.1:8b, the share of statements that match their sources,
averaged over answers. Moving the code must leave every metric unchanged. The
hand-computed metric tests still pass, and a one-off comparison of the old and new code
on the 1,309 answers in the existing reports (3,927 answer and passage combinations) gave
identical results.

## Not in this version

A tamper-evident chain of receipts; a printable or signed receipt; marking statements
inside the answer text itself; asking again from an old answer with one click; checks by
a model (entailment) rather than by words and numbers.

## Anti-goals

Never present the lexical check as proof that an answer is correct. Never show a receipt
for text the reader may no longer see. Never let an old answer look current after its
source was replaced.
