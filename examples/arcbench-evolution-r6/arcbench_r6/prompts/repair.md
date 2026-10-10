Repair the next dependency frontier in the existing implementation.
Coordinator-selected targets (blocked means the feature assertion was not reached):
@TARGETS@
Overall evidence:
@COVERAGE@

Address all listed reachable failures, including compatibility checks even when scenario coverage
is complete. A runtime error need not block fixing an independent failed assertion in the same pass.
Check only these paths and their relevant source; preserve working features. Startup on the inherited DB must expose /api/health only after schema and seed complete. Restarting must preserve existing DB records and contents. For a browser error, fix its route/render/API cause. The coordinator repeats
the final check after your repair, so do not repeat the whole delivery procedure yourself.
Read the failure's source requirement cards and their ancestor_cards before editing. Fix shared
components causing multiple reported failures together; do not add links visible to unauthorized
users or weaken validation just to satisfy a locator. Keep the prescribed starting page and roles.
Full browser failures are in .arc/checks/browser-report.json. Fix shared entry blockers first;
their removal may reveal NEW failures. A new failure after an entry fix is not evidence the
repair failed. Use run_acceptance for affected IDs to verify the real path after editing.
Backend logs are in .arc/checks/backend.log, separated by startup/restart stage. Read the relevant
tail first. If a 500 response hides the exception, log its cause in a disposable reproduction before
guessing a fix. Do not reread an unchanged whole file or rediscover platform harness files.
Use existing coordinator checks, not another self-written substitute suite. Do not claim a
dependent feature is verified merely because a shared component was edited. Record checkpoints.
When full scenarios pass but stability_confirmation fails, read .arc/checks/stability-report.json
and its referenced fresh-copy reports: they contain the failing operation and requirement IDs.
Repair the asynchronous navigation/save cause; don't lengthen waits or weaken assertions.
Skipped paths are not application failures or verified paths. Preserve previously passing paths.
Finish with three brief lines: Unfinished: original IDs or none; Blockers: remaining blockers;
Files: key implementation paths.