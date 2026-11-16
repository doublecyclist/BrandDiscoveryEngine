# Screening recommendations and saved user decisions

Milestone 1 separates automated screening from permanent user choices.
The engine may recommend Reject, Investigate or Shortlist based on evidence,
but it does not save those recommendations as decisions.

Only explicit user actions save:

- Keep (`Keep` in the existing JSON format)
- Investigate (`Maybe`)
- Discard (`Reject`)

Existing decisions are not migrated or reclassified, including historical
`decision_source: automatic`, manual `Undecided`, and uncertain legacy provenance.
The compatibility `sync_automatic_decisions` entry point is now a no-op, and the
app no longer invokes it on startup.

Dashboard decision counts and Keep/Investigate/Discard filters read saved choices.
Conflict and automatic-recommendation filters remain separate evidence views.
Historical automatic choices are explicitly labeled, not silently rewritten.
Finalist selection is deferred to a later milestone.

Startup, filtering and decision changes use saved evidence only. Provider requests
occur only after the user explicitly starts research. No existing research,
pricing or candidate data is deleted by screening.

Run focused regression tests from the project folder:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
```

These are screening recommendations, not trademark clearance.
