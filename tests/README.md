Temporary systemd test helpers for the failed-services task manager UI.

Files:
- `seed_failed_service.sh`: creates a runtime-only systemd unit in `/run/systemd/system`, starts it, and leaves it in the `failed` state on purpose.
- `cleanup_failed_service.sh`: removes the runtime unit and clears its failed state.
- `failing_service_payload.py`: the Python payload that raises an exception so the journal contains a traceback-like failure record.

Usage:

```bash
bash tests/seed_failed_service.sh
```

Cleanup:

```bash
bash tests/cleanup_failed_service.sh
```

Notes:
- These scripts require a host with systemd and working `sudo`.
- The generated unit is runtime-only, so it does not persist across reboot.
- You can override the unit name by passing it as the first argument to either script.
