#!/usr/bin/env bash
set -euo pipefail

mkdir -p /tmp/astinuslab-sample
echo "sample workload started" > /tmp/astinuslab-sample/status.txt
dd if=/dev/urandom of=/tmp/astinuslab-sample/blob.bin bs=1M count=8 status=none
# The single quotes are load-bearing: $(date -Is) must run once per tick inside
# the background shell, not once here at spawn time.
# shellcheck disable=SC2016
nohup bash -c 'while true; do echo "$(date -Is) tick" >> /tmp/astinuslab-sample/activity.log; sleep 2; done' >/tmp/astinuslab-sample/nohup.out 2>&1 &
# Debian/Ubuntu name the unit ssh; Fedora names it sshd. Retain a nonzero
# workload result if neither service is active.
systemctl is-active ssh >/tmp/astinuslab-sample/ssh.status || systemctl is-active sshd >/tmp/astinuslab-sample/ssh.status
echo "sample workload completed"
