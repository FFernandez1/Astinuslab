#!/usr/bin/env bash
set -x
# List all astinuslab VMs
vms=$(virsh -c qemu:///system list --all --name | grep "^astinuslab-")

for vm in $vms; do
  virsh -c qemu:///system destroy "$vm" || true
  virsh -c qemu:///system undefine "$vm" --nvram || virsh -c qemu:///system undefine "$vm" || true
done

# Clear DB. Uses the app engine rather than the sqlite3 CLI, which is not a
# project prerequisite: on hosts without it this wipe silently did nothing and
# left job_runtimes rows that read as a permanently active forensic job.
.venv/bin/python -m astinuslab.dbcli clear-jobs

# Drain the Celery broker. Clearing job rows alone leaves queued tasks (e.g.
# teardowns) in the sqlite broker; on the next worker start they replay against
# now-missing domains and can starve new work. Stop workers before running this.
if [ -f storage/celery_broker.db ]; then
  .venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('storage/celery_broker.db')
conn.execute('DELETE FROM kombu_message')
conn.commit()
conn.close()
" || true
fi

# Reset network
virsh -c qemu:///system net-destroy default || true
virsh -c qemu:///system net-start default || true
