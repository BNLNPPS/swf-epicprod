#!/bin/bash
# Bounded worker-side startup diagnostic. Never dumps credentials or environment.
set -u
date -u +'%Y-%m-%dT%H:%M:%SZ'
hostname -f
printf 'slurm_job=%s memory_mb=%s\n' "${SLURM_JOB_ID:-unknown}" "${SLURM_MEM_PER_NODE:-unknown}"
python3 --version
python3 - <<'PY'
import importlib.util, socket
for module in ('ssl', 'psutil', 'requests', 'rucio', 'XRootD'):
    print('python_module', module, bool(importlib.util.find_spec(module)))
for host in ('pandaserver01.sdcc.bnl.gov', 'nprucio01.sdcc.bnl.gov', 'dtn-rucio.jlab.org'):
    try:
        print('dns', host, sorted({a[4][0] for a in socket.getaddrinfo(host, None)}))
    except Exception as error:
        print('dns_error', host, str(error))
PY
for path in /cvmfs/eic.opensciencegrid.org/panda/pilot/canary/pilot3.tar.gz \
    /cvmfs/oasis.opensciencegrid.org/mis/certificates \
    /cvmfs/singularity.opensciencegrid.org /cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase/user/atlasLocalSetup.sh; do
    if test -r "$path"; then printf 'readable %s\n' "$path"; else printf 'unavailable %s\n' "$path"; fi
done
command -v apptainer singularity xrdcp || true
proxy=${X509_USER_PROXY:-$PWD/pilot-proxy.pem}
if test -r "$proxy"; then
    openssl x509 -in "$proxy" -noout -dates
else
    printf 'proxy_unreadable %s\n' "$proxy"
fi
for family in -4 -6; do
    for url in https://pandaserver01.sdcc.bnl.gov:25443/server/panda/isAlive \
        https://pandaserver01.sdcc.bnl.gov:443/server/panda/isAlive \
        https://nprucio01.sdcc.bnl.gov:443/ping https://rucio-server.jlab.org/ping; do
        printf 'connect %s %s\n' "$family" "$url"
        curl "$family" --noproxy '*' --connect-timeout 5 --max-time 8 -sS -o /dev/null \
            -w 'http=%{http_code} remote=%{remote_ip} connect=%{time_connect} tls=%{time_appconnect}\n' \
            --capath /cvmfs/oasis.opensciencegrid.org/mis/certificates "$url" || true
    done
done
date -u +'%Y-%m-%dT%H:%M:%SZ'
