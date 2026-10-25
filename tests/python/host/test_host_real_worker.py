from __future__ import annotations


def test_real_host_worker_consumes_default_and_analysis_queues():
    from astinuslab.jobs.celery_app import celery_app
    from tests.python._support.helpers import real_host_worker_command

    routed_queue = celery_app.conf.task_routes["run_vol3_plugin"]["queue"]
    command = real_host_worker_command()
    queue_arg = command[command.index("--queues") + 1]

    assert set(queue_arg.split(",")) == {"celery", routed_queue}
