#!/bin/bash
# Read or set the ePIC job throttler's MODE row for one VO in the PanDA
# config table (component epic_job_throttler, app jedi;
# docs/EPIC_JOB_THROTTLER.md). Runs on pandaserver01, which holds the
# database credential:
#
#   ssh pandaserver01 'bash -s' < scripts/jedi-throttler-mode.sh            # show the rows
#   ssh pandaserver01 'bash -s' -- wlcg observe < scripts/jedi-throttler-mode.sh
set -euo pipefail

CFG=/etc/panda/panda_server.cfg
export PGPASSWORD=$(awk -F' = ' '/^ *dbpasswd *=/{print $2; exit}' "$CFG")
PSQL=(psql -X -v ON_ERROR_STOP=1 -h pandadb01.sdcc.bnl.gov -U panda -d panda_db)
SHOW="select vo, key, value from doma_panda.config where component='epic_job_throttler' and app='jedi' order by vo, key"

if [ $# -eq 0 ]; then
    "${PSQL[@]}" -c "$SHOW"
    exit 0
fi

VO=${1:?vo}
MODE=${2:?mode}
case "$VO" in wlcg|epic) ;; *) echo "unknown vo: $VO" >&2; exit 2 ;; esac
case "$MODE" in observe|throttle) ;; *) echo "mode must be observe or throttle: $MODE" >&2; exit 2 ;; esac

"${PSQL[@]}" <<SQL
begin;
update doma_panda.config set value='$MODE'
 where component='epic_job_throttler' and app='jedi' and key='MODE' and vo='$VO';
do \$\$ begin
  if (select count(*) from doma_panda.config
      where component='epic_job_throttler' and app='jedi' and key='MODE' and vo='$VO' and value='$MODE') <> 1 then
    raise exception 'MODE row for vo $VO not set to $MODE';
  end if;
end \$\$;
commit;
$SHOW;
SQL
