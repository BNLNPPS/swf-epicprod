#!/bin/bash
# Read-only filesystem evidence from a GREX batch worker. No production work.
set -u
date -u +%FT%TZ
hostname
id
for path in "$PWD" "${OSG_WN_TMP:-}" "${SLURM_TMPDIR:-}" "/scratch/slurm/${SLURM_JOB_ID:-}" /tmp /scratch; do
    [ -n "$path" ] && [ -d "$path" ] || continue
    echo "SPACE_PATH=$path"
    df -B1 -P "$path"
    df -Pi "$path"
    if command -v findmnt >/dev/null; then
        findmnt -T "$path" -n -o SOURCE,FSTYPE,TARGET
    fi
    if [ -w "$path" ]; then echo WRITABLE=yes; else echo WRITABLE=no; fi
done
printf 'OSG_WN_TMP=%s SLURM_TMPDIR=%s SLURM_JOB_ID=%s\n' "${OSG_WN_TMP:-}" "${SLURM_TMPDIR:-}" "${SLURM_JOB_ID:-}"
