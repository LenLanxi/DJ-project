# Scenario acceptance tests

Offline acceptance and fault-injection tests for the walking-user / red-tent /
full-turn / resume-follow scenario, including the requested AI vision in
`docs/v4-reference`. The accompanying Realtime and sandbox code is assessed as
an integration candidate; these tests do not choose or migrate the application
architecture.

The suite preserves the review identifiers **T01–T43**. `Cases.json` maps every
identifier to its requirement, source locations and reviewed expectation.
The initial review baseline is commit `3e57a36791053f5b8b6dc18b026889c568254cb8`.
This change adds tests, not flight-control fixes.

## Run

From the repository root, using Python 3.11 and the existing pinned image
dependencies:

```powershell
uv run --no-project --python 3.11 --with numpy==1.26.4 --with opencv-python==4.8.0.76 python -B ScenarioAcceptance/ScenarioTests.py --all
```

`--all` runs the existing root/AI/follow offline suites, the ground **self-check**,
the existing docs suite, and both new acceptance groups in separate processes.
It never starts a ground hardware test. The original three Docker tests remain
explicitly disabled; cloud credentials, microphones and aircraft are not used.
No container or model-generated Python is executed on the host.

For just the new groups:

```powershell
uv run --no-project --python 3.11 --with numpy==1.26.4 --with opencv-python==4.8.0.76 python -B ScenarioAcceptance/ScenarioTests.py
uv run --no-project --python 3.11 --with numpy==1.26.4 --with opencv-python==4.8.0.76 python -B ScenarioAcceptance/DocsTests.py
```

Add `--strict` to either command to report known gaps as ordinary assertion
failures. This retains the original failure reproductions. `--only DocsAcceptance`
or `--only ScenarioAcceptance` selects a subprocess group in the main runner.

## Results and interpretation

- `pass`: a supported behavior or control assertion passed.
- `known_gap` / XFAIL: an assertion failed for an item recorded in `Cases.json`.
- `resolved_gap`: an assertion previously recorded as a gap now passes. This is
  an unexpected success and exits nonzero: review the fix and update the catalog.
- `fail`: an unrecorded assertion failure, or any assertion failure in strict mode.
- `error`: a test could not execute normally. Errors are never hidden as XFAIL.

At the review baseline the 43 new cases yield **11 passes and 32 known gaps**.
Known gaps include implementation defects, missing mission requirements,
protocol hardening and explicit faulty-model injection. They are not 32
independent product bugs or a statistical flight/model failure rate.
Expected failures are matched by case identifier, so inspect their details:
a different assertion failure in the same case still needs investigation.

JSON results are written to the ignored `ScenarioAcceptance/Results/` directory.
They include the tested Git revision and per-case details. New generated logs
are not committed. Default runs exit zero when results match the reviewed
baseline. Strict runs exit nonzero while gaps remain.

## Archived review evidence

The requested historical review records are committed under `Evidence/`,
separately from the ignored, regenerated `Results/` directory:

| File | Contents |
| --- | --- |
| [DocsResults.json](Evidence/DocsResults.json) | T20–T43: 24 cases, 4 passes, 20 assertion failures, 0 execution errors; includes per-case failure details and T34's stale-grant reproduction. |
| [DocsBaseline.json](Evidence/DocsBaseline.json) | Original docs suite: 54 collected, 51 passes and 3 Docker tests skipped. |
| [BaselineResults.json](Evidence/BaselineResults.json) | Root/AI/follow and ground self-check records, plus the two acceptance groups; raw acceptance runs intentionally exit 1. |

These files are byte-for-byte copies of the requested local review records
collected against business-code baseline
`3e57a36791053f5b8b6dc18b026889c568254cb8`. Their original JSON schema, console
output and historical Windows traceback paths are preserved. They are evidence,
not inputs to the tests, and do not require that machine or directory to run.
The archived results predate the portable runner's XFAIL reporting. A raw
`failed` result in these records corresponds to a catalogued `known_gap` in the
current default runner; it does not mean the business defect was fixed. Use
`--strict` to reproduce ordinary assertion failures with the current scripts.
The old scope label "Required docs route" identifies the audit group; the
required component is docs AI vision, while Realtime/sandbox adoption remains
an integration decision. Archived output does not establish real-flight,
live-model or Docker acceptance.

The requested `ScenarioTests.py` and `DocsTests.py` are already committed in this
directory as portable versions. Historical machine-specific scripts are not
copied over them. Normal runs never overwrite `Evidence/`.

## T34: old task reuses a new same-owner grant

`DocsTests.py`, method
`GatewayTests.test34_old_docs_action_cannot_use_new_same_owner_grant`, reproduces
the control handoff defect at `docs/v4-reference/backends.py`, `SDKBackend.action`:

```python
getattr(self.sdk, name)('model', *args)
```

The test delays an old action, interrupts it, releases control, reacquires
control under the same `model` owner, and then releases the old action's delay.
It asserts that no forward action is sent. The current adapter omits
`control_epoch` and `command_epoch`, so the assertion fails even though the
controller already supports both checks. This test does not fix the defect.

## Limits

Images are synthetic and device/model/socket responses are explicit fakes.
Tests do not establish physical stopping, actual landing, obstacle clearance,
distance to a tent, user reacquisition, audio recognition quality, API protocol
compatibility with a live account, or real Docker isolation. Those need separate
integration and controlled physical acceptance.
