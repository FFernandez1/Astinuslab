#!/usr/bin/env bash
set -e
# 1. Stop services
./astinuslab.sh stop

# 2. Wiping storage
echo "Wiping storage directories..."
rm -rf storage/logs/* storage/tmp/* storage/dumps/* storage/isf-matrix-runs/* storage/workload-cache/*
touch storage/logs/.gitkeep storage/tmp/.gitkeep storage/dumps/.gitkeep storage/isf-matrix-runs/.gitkeep storage/workload-cache/.gitkeep

# 3. Stray VMs
vms=$(virsh -c qemu:///system list --all --name | grep "^astinuslab-") || true
for vm in $vms; do
  virsh -c qemu:///system destroy "$vm" || true
  virsh -c qemu:///system undefine "$vm" --nvram || virsh -c qemu:///system undefine "$vm" || true
done

# 4. Network
virsh -c qemu:///system net-destroy default || true
virsh -c qemu:///system net-start default || true

# 5. Database
rm -f storage/astinuslab.db
./.venv/bin/python -c "from astinuslab.db import initialize_database; initialize_database()"
echo "Cleanup complete."
