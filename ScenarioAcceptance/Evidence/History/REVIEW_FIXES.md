# 2026-10-24 review repair candidate

Base: 9aaec59. Work is isolated on fix/group4-review. This document records software changes, not flight clearance.

## Runtime

The canonical vision, gateway, sandbox and Realtime components now live in modules/ai.
The docs copies are compatibility imports, not independent implementations.
From the repository root:

```
python -m pip install -r modules/ai/requirements.txt
# Also install the pinned root vision requirements for SDK camera/follow work.
docker build -t companion-sandbox:0.4 modules/ai/sandbox
python modules/ai/main.py --program docs/v4-reference/examples/red_tent.py --fixture-vision
python modules/ai/main.py --realtime --audio-config audio_config.json
```

Realtime uses AI_REALTIME_URL, AI_REALTIME_API_KEY and AI_REALTIME_MODEL. Configure an independently provisioned service implementing the session/audio/tool WebSocket protocol used by realtime.py. Text/vision use AI_CHAT_URL, AI_API_KEY and AI_MODEL. Setting a domestic provider's URL is not evidence its protocol is compatible; a live-account test is required. No provider address is hardcoded and no vision-key fallback is used.

The old --always-on path is retained as a sequential STT/planner/TTS compatibility mode. It pauses listening during playback; it is NOT the full duplex acceptance route. Use --realtime for full duplex. A provider without a compatible realtime protocol requires a dedicated adapter; it is not silently downgraded.

The synchronous planner admits complete Python through an explicit sandbox-enabled Runner. accepted means admission/syntax support, not execution success. Check ok/status and journal. With no Docker it returns ok=false/status=unavailable and does not execute on the host. Existing direct-call demo remains a limited compatibility path. The 50cm example now works there; old tests asserting 50cm/100cm rejection were adjusted for this changed distance requirement. Teacher's T01-T43 assertions were not changed.

## Repairs

- Shared takeoff permission/battery gate; unknown, invalid and low battery rejected even via SDK adapter.
- Real status read from driver, mode propagated; real landing completion no longer uses a simulation timer.
- Unfinished driver workers keep an ownership barrier; deferred landing retries rather than dropping the request.
- Failed RC/cleanup receipts propagate, failed cleanup retains ownership state.
- Real entry attempts video startup and publishes shared frames; structured Vision is connected to Agent.
- Repeated cached frames keep identity/time. The av reader refuses expired cached frames. Non-av capture identity still depends on the reader's contract; field validation is required.
- Visual result age rechecked after inference and before navigation; visible zero-width and contradictory invisible coordinates rejected; static pictures cannot drive real navigation.
- Normal task stops preserve follow restoration; user interrupt invalidates it. Pause/resume tickets preserve reference size; restoration waits for a new frame.
- Model actions carry captured control/command epochs. R3 velocity includes command epoch, grant confirmation checks its original epoch, telemetry failure still attempts lease-qualified stopping.
- Closed follow services with unconfirmed cleanup are retained/retried before restarting.
- Local interruption is dispatched before network cancellation; queued new audio is not truncated as an already-consumed old item.
- Explicit prohibition phrases are blocked independently of model output. This covers the supplied negative-command injection, not a proof of general natural-language intent correctness.
- Sample includes fixture-only 360-degree rotation and follow restart; real operation refuses to treat bounding-box occupancy as arrival evidence.
- Unsafe motor/RC/physical-response claims and routine grabbing/battery-removal advice removed from the emergency SOP.

Source digest checking stays enabled. sdk_consolidated_pins.json is updated for the modified shared source; historical review manifests are preserved as historical evidence.

## Remaining acceptance boundaries

Passing T01-T43 only proves the assertions in those offline probes. T06-T08 check program admission; they do not execute Docker or a complete mission. No Docker executable, aircraft, microphone, or configured live provider is available here.

Independent measured target distance, obstacle clearance and finding a user outside the camera view are not supplied by the current sensors/API. The code deliberately stops/refuses unsupported real arrival instead of claiming these requirements are implemented.

Still required: actual container isolation/cancellation cleanup, representative audio recordings and real earphone barge-in, domestic provider protocol/latency validation, UDP late/duplicate-response tests, independent distance/obstacle evidence, whole-mission feedback scenarios, and controlled physical tests. These are not replaced with mocked success.
