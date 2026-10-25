"""Sequential first-boot evidence; no VM or guest operations at import time."""


def require(value):
    if not value:
        raise ValueError("firstboot_pair")


def identity(result):
    require(result["result"] == "UNATTENDED_FIRSTBOOT_OBSERVED" and not result["failure_phases"])
    require(
        result["clean_shutdown"] is True
        and result["all_domains_off"] is True
        and result["qemu_processes"] == 0
        and result["protected_state_unchanged"] is True
        and result["candidate_may_be_running"] is False
    )
    rows = [r["value"] for r in result["observations"] if r["stage"] == "post_setup"]
    require(len(rows) == 1)
    return rows[0]


def run(host):
    result = {
        "result": "STOP",
        "scope": "two_unattended_firstboots",
        "failure_phases": [],
        "attempt_may_exist": False,
        "start_supervision_attempted": [],
        "clones": {},
        "closure_confirmed": False,
        "result_recorded": False,
        "artifacts_retained": True,
        "source_modified": False,
        "generalization_performed": False,
        "reusable_image_accepted": False,
        "encryption_state": "unknown",
        "machine_sid_prefixes_distinct": False,
        "initial_computer_names_distinct": None,
        "candidate_may_be_running": False,
        "all_domains_off": None,
    }
    phase = "prepare"
    try:
        host.prepare()
        phase = "claim"
        result["attempt_may_exist"] = True
        host.claim()
        for label in ("a", "b"):
            phase = "boot_" + label
            result["start_supervision_attempted"].append(label)
            result["candidate_may_be_running"] = True
            result["all_domains_off"] = None
            observed = host.boot(label)
            result["clones"][label] = observed
            result["candidate_may_be_running"] = observed["candidate_may_be_running"]
            result["all_domains_off"] = observed["all_domains_off"]
            identity(observed)
            phase = "complete_" + label
            host.complete(label, observed)
        phase = "identities"
        first, second = (identity(result["clones"][label]) for label in ("a", "b"))
        require(first["machine_sid_prefix"] != second["machine_sid_prefix"])
        result["machine_sid_prefixes_distinct"] = True
        result["initial_computer_names_distinct"] = (
            first["computer_name"].casefold() != second["computer_name"].casefold()
        )
    except BaseException:
        result["failure_phases"].append(phase)
    finally:
        try:
            host.close()
            result["closure_confirmed"] = True
        except BaseException:
            result["failure_phases"].append("close")
        if not result["failure_phases"]:
            result["result"] = "PAIR_UNATTENDED_FIRSTBOOT_OBSERVED"
        try:
            host.record(result)
            result["result_recorded"] = True
        except BaseException:
            result["failure_phases"].append("record")
            result["result"] = "STOP"
    return result
