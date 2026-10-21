#!/bin/bash
# Read or set one ePIC job throttler configuration row in the PanDA config
# table (component epic_job_throttler, app jedi; docs/EPIC_JOB_THROTTLER.md,
# the configuration table). Runs on pandaserver01, which holds the
# database credential. The MODE row has its own script,
# jedi-throttler-mode.sh.
#
#   ssh pandaserver01 'bash -s' < scripts/jedi-throttler-config.sh             # show the rows
#   ssh pandaserver01 'bash -s' -- wlcg NQUEUELIMIT_osg 3123 int "queued floor of the OSG pool" < scripts/jedi-throttler-config.sh
set -euo pipefail

CFG=/etc/panda/panda_server.cfg
export PGPASSWORD=$(awk -F' = ' '/^ *dbpasswd *=/{print $2; exit}' "$CFG")
PSQL=(psql -X -v ON_ERROR_STOP=1 -h pandadb01.sdcc.bnl.gov -U panda -d panda_db)
SHOW="select vo, key, value, type from doma_panda.config where component='epic_job_throttler' and app='jedi' order by vo, key"

if [ $# -eq 0 ]; then
    "${PSQL[@]}" -c "$SHOW"
    exit 0
fi

VO=${1:?vo}
KEY=${2:?key}
VALUE=${3:?value}
TYPE=${4:-str}
DESCR=${5:-ePIC job throttler: $KEY}
case "$VO" in wlcg|epic) ;; *) echo "unknown vo: $VO" >&2; exit 2 ;; esac
case "$KEY" in MODE) echo "MODE has its own script, jedi-throttler-mode.sh" >&2; exit 2 ;; esac
[[ "$KEY" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad key: $KEY" >&2; exit 2; }
VALUE_RE='^[A-Za-z0-9_.,:;-]+$'
[[ "$VALUE" =~ $VALUE_RE ]] || { echo "bad value: $VALUE" >&2; exit 2; }
case "$TYPE" in str|int|float) ;; *) echo "type must be str, int or float" >&2; exit 2 ;; esac
DESCR=${DESCR//\'/}

"${PSQL[@]}" <<SQL
begin;
update doma_panda.config set value='$VALUE', type='$TYPE', descr='$DESCR'
 where component='epic_job_throttler' and app='jedi' and key='$KEY' and vo='$VO';
insert into doma_panda.config (app, component, vo, key, value, type, descr)
 select 'jedi', 'epic_job_throttler', '$VO', '$KEY', '$VALUE', '$TYPE', '$DESCR'
 where not exists (select 1 from doma_panda.config
   where component='epic_job_throttler' and app='jedi' and key='$KEY' and vo='$VO');
commit;
$SHOW;
SQL
