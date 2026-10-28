#!/bin/bash
# ePIC test probe for the Fir ARC CE (CA-SFU-T2): what a pilot would land
# on. Prints facts only; touches nothing.
echo "== identity"
date -u +%FT%TZ
hostname -f 2>/dev/null || hostname
id
echo "== batch"
env | grep -E '^(GRID_GLOBAL_JOBID|GRID_GLOBAL_JOBURL|ARC_|NORDUGRID_|SLURM_JOB_ID|SLURM_CPUS_ON_NODE|SLURM_MEM_PER_CPU|SLURM_JOB_PARTITION|SLURM_JOB_ACCOUNT|RUNTIME_|X509_USER_PROXY|BEARER_TOKEN_FILE)=' | sort
echo "cores visible: $(nproc)"
grep -E 'MemTotal' /proc/meminfo
cat /sys/fs/cgroup/memory.max 2>/dev/null | sed 's/^/cgroup memory.max: /'
echo "== os"
grep -E '^(NAME|VERSION_ID)=' /etc/os-release
uname -r
echo "== cvmfs"
for repo in eic.opensciencegrid.org singularity.opensciencegrid.org atlas.cern.ch sft.cern.ch oasis.opensciencegrid.org; do
  if ls /cvmfs/${repo}/ >/dev/null 2>&1; then echo "${repo}: mounted"; else echo "${repo}: MISSING"; fi
done
echo "== containers"
for c in apptainer singularity; do command -v ${c} >/dev/null && echo "${c}: $(${c} --version 2>&1 | head -1)"; done
echo "unprivileged user namespaces: $(cat /proc/sys/user/max_user_namespaces 2>/dev/null)"
if command -v apptainer >/dev/null && ls /cvmfs/singularity.opensciencegrid.org/ >/dev/null 2>&1; then
  img=/cvmfs/singularity.opensciencegrid.org/opensciencegrid/osgvo-el9:latest
  timeout 300 apptainer exec ${img} bash -c 'echo "apptainer exec osgvo-el9: ok ($(grep PRETTY_NAME /etc/os-release))"' 2>&1 | tail -2
fi
echo "== network"
for url in https://pandaserver01.sdcc.bnl.gov:25443 https://epicxrd1.sdcc.bnl.gov:1094; do
  printf '%s: ' "${url}"; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "${url}" || echo unreachable
done
echo "== disk"
df -h . | tail -1
echo "== done"
