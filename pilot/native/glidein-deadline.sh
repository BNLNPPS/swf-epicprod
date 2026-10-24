# Sourced by epic-native-pilot.sh, with set -euo pipefail in force: the glidein's
# death time as the deadline the pilot plans against. Nothing here exits.
#   Inputs:  HERE (the sandbox), _CONDOR_MACHINE_AD (this execution's machine ad),
#            MACHINEFEATURES (a site's HEPiX directory, optional),
#            EPICPROD_MIN_TIME_FOR_NEW_JOB (optional override, positive integer seconds).
#   Outputs: DEADLINE_VERDICT ok|short|unknown, DEADLINE_LEFT seconds (empty when unknown),
#            DEADLINE_AT epoch (empty when unknown), MACHINEFEATURES exported at
#            $HERE/machinefeatures carrying the earlier of the site's shutdowntime and
#            GLIDEIN_ToDie (and the site's other feature files), EPICPROD_MIN_TIME_FOR_NEW_JOB
#            exported validated.
# The minimum life for a job: the job's p99 of 7200 s (23 October 2026) + the pilot
# monitor's 600 s shutdown grace + 900 s for stage-out and registration + 300 s
# setup = 9000 s. The pilot applies the same number before every further job.
deadline_now=$(date +%s)
deadline_min=${EPICPROD_MIN_TIME_FOR_NEW_JOB:-}
if [[ -r $HERE/runtime-policy.py ]]; then
    deadline_min=$(python3 "$HERE/runtime-policy.py" \
        --policy "${EPICPROD_RUNTIME_POLICY:-$HERE/queue-runtime-policy.json}" \
        --queue "${PILOT_SITENAME:-}" --machine-ad "${_CONDOR_MACHINE_AD:-}")
fi
[[ $deadline_min =~ ^[1-9][0-9]*$ ]] || deadline_min=9000
export EPICPROD_MIN_TIME_FOR_NEW_JOB=$deadline_min
deadline_todie=""
if [[ -n ${_CONDOR_MACHINE_AD:-} && -r ${_CONDOR_MACHINE_AD} ]]; then
    deadline_todie=$(awk '$1 == "GLIDEIN_ToDie" && $2 == "=" { print $3; exit }' "${_CONDOR_MACHINE_AD}" 2>/dev/null || true)
fi
[[ $deadline_todie =~ ^[0-9]+$ ]] || deadline_todie=""
deadline_site=""
deadline_sitedir=${MACHINEFEATURES:-}
if [[ -n $deadline_sitedir && -r $deadline_sitedir/shutdowntime && $deadline_sitedir != "$HERE/machinefeatures" ]]; then
    deadline_site=$(tr -d '[:space:]' < "$deadline_sitedir/shutdowntime" 2>/dev/null || true)
fi
[[ $deadline_site =~ ^[0-9]+$ ]] || deadline_site=""
DEADLINE_AT=""
if [[ -n $deadline_todie && -n $deadline_site ]]; then
    DEADLINE_AT=$(( deadline_todie < deadline_site ? deadline_todie : deadline_site ))
elif [[ -n $deadline_todie ]]; then DEADLINE_AT=$deadline_todie
elif [[ -n $deadline_site ]]; then DEADLINE_AT=$deadline_site
fi
if [[ -n $DEADLINE_AT ]]; then
    mkdir -p "$HERE/machinefeatures"
    if [[ -n $deadline_sitedir && -d $deadline_sitedir && $deadline_sitedir != "$HERE/machinefeatures" ]]; then
        cp -n "$deadline_sitedir"/* "$HERE/machinefeatures/" 2>/dev/null || true
    fi
    echo "$DEADLINE_AT" > "$HERE/machinefeatures/shutdowntime"
    export MACHINEFEATURES=$HERE/machinefeatures
    DEADLINE_LEFT=$(( DEADLINE_AT - deadline_now ))
    if (( DEADLINE_LEFT >= deadline_min )); then DEADLINE_VERDICT=ok; else DEADLINE_VERDICT=short; fi
else
    # No fresh deadline: a generated directory left from an earlier execution
    # must not stand in for one; a site's own directory stays as it was.
    if [[ ${MACHINEFEATURES:-} == "$HERE/machinefeatures" ]]; then
        rm -f "$HERE/machinefeatures/shutdowntime"
        unset MACHINEFEATURES
    fi
    DEADLINE_LEFT=""
    DEADLINE_VERDICT=unknown
fi
