"""Guarded shutdown of an existing failed trial; no import-time host actions."""

import copy


def require(value, phase):
    if not value:
        raise ValueError(phase)


def run(adapter, expected_boot):
    result = {
        "result": "STOP",
        "scope": "runtime_a_guarded_shutdown",
        "failure_phases": [],
        "windows_started": False,
        "shutdown_attempted": False,
        "shutdown_acknowledged": False,
        "clean_shutdown": False,
        "candidate_may_be_running": True,
        "reusable_image_accepted": False,
        "acl_acceptance_passed": False,
        "encryption_state": "unknown",
        "observations": [],
    }
    phase = "initial"
    try:
        require(adapter.snapshot()["active"] is True, "already_off")
        phase = "setup_first"
        first = adapter.setup()
        result["observations"].append(first)
        require(first["boot_id"] == expected_boot, "boot_changed")
        adapter.sleep(2)
        phase = "setup_second"
        second = adapter.setup()
        result["observations"].append(second)
        adapter.same_boot(first, second)
        phase = "shutdown"
        result["shutdown_attempted"] = True
        adapter.shutdown(second)
        result["shutdown_acknowledged"] = True
        phase = "shutdown_wait"
        deadline = adapter.clock() + 180
        while adapter.clock() < deadline:
            view = adapter.snapshot()
            if not view["active"]:
                require(view["state"] == [5, 1] and view["qemu_processes"] == 0, "shutdown_reason")
                result["clean_shutdown"] = True
                break
            adapter.sleep(2)
        require(result["clean_shutdown"], "shutdown_deadline")
    except BaseException:
        result["failure_phases"].append(phase)
    try:
        view = adapter.snapshot()
        result["final_snapshot"] = view
        result["candidate_may_be_running"] = view["active"]
    except BaseException:
        result["failure_phases"].append("final_snapshot")
    result["probe_failure"] = copy.deepcopy(getattr(adapter, "probe_failure", None))
    if not result["failure_phases"]:
        result["result"] = "A_SHUT_DOWN_AFTER_FIRSTBOOT_STOP"
    return result
