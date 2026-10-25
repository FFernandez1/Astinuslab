"""Cold clone preparation policy. Caller supplies reviewed host operations.

No import-time I/O, VM start, registration, cleanup, or approval is provided here.
Successful preparation is not native Windows or reusable-image acceptance.
"""


def require(value, phase):
    if not value:
        raise ValueError(phase)


def validate_image(value):
    require(
        type(value) is dict
        and value.get("format") == "qcow2"
        and type(value.get("virtual-size")) is int
        and value["virtual-size"] == 40 * 1024**3,
        "image_format",
    )
    require(
        not any(
            k in value for k in ("backing-filename", "full-backing-filename", "backing-filename-format", "data-file")
        )
        and not value.get("snapshots")
        and value.get("encrypted", False) is False,
        "image_external",
    )
    detail = value.get("format-specific")
    require(type(detail) is dict and detail.get("type") == "qcow2", "image_detail")
    data = detail.get("data")
    require(
        type(data) is dict
        and data.get("corrupt") is False
        and not any(k in data for k in ("data-file", "data-file-raw", "encrypt", "encrypted")),
        "image_detail_data",
    )


def run(host):
    result = {
        "result": "STOP",
        "scope": "two_cold_runtime_clones",
        "failure_phases": [],
        "attempt_may_exist": False,
        "allocation_attempted": [],
        "copies_verified": [],
        "definition_attempted": [],
        "domains_defined": [],
        "artifacts_retained": True,
        "source_modified": False,
        "windows_booted": False,
        "generalization_performed": False,
        "reusable_image_accepted": False,
        "encryption_state": "unknown",
        "all_domains_off": None,
        "qemu_processes": None,
        "protected_state_unchanged": False,
        "closure_confirmed": False,
        "result_recorded": False,
    }
    ready = False
    phase = "prepare"
    try:
        host.prepare()
        ready = True
        phase = "preflight"
        host.preflight()
        phase = "claim"
        result["attempt_may_exist"] = True
        host.claim()
        for label in ("a", "b"):
            phase = "allocate_" + label
            result["allocation_attempted"].append(label)
            host.allocate(label)
            result["copies_verified"].append(label)
        for label in ("a", "b"):
            phase = "stage_" + label
            host.stage(label)
        phase = "verify"
        host.verify()
        for label in ("a", "b"):
            phase = "define_" + label
            result["definition_attempted"].append(label)
            host.define(label)
            result["domains_defined"].append(label)
        phase = "verify"
        host.verify()
    except BaseException:
        result["failure_phases"].append(phase)
    finally:
        if ready:
            try:
                result.update(host.snapshot())
            except BaseException:
                result["failure_phases"].append("snapshot")
        try:
            host.close()
            result["closure_confirmed"] = True
        except BaseException:
            result["failure_phases"].append("close")
        if not result["failure_phases"]:
            result["result"] = "COLD_RUNTIME_PAIR_PREPARED"
        try:
            host.record(result)
            result["result_recorded"] = True
        except BaseException:
            result["failure_phases"].append("record")
            result["result"] = "STOP"
    return result
