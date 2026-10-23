# Sourced by epic-native-pilot.sh (set -euo pipefail in force). The Rucio client
# names its token cache after the user through getpass.getuser(), which reads
# LOGNAME, USER, LNAME, USERNAME and then the passwd entry of the uid; a glidein
# uid with no passwd entry inside the image and no such variable (UA-LR, 23
# October 2026, job 5816712) leaves it nothing and the upload crashes. Export a
# name: the one in the environment, else the passwd name, else uid<n>.
# `id -un` may print the number and still fail, so its output is used only
# when it succeeds.
if [[ -z ${USER:-} ]]; then
    if job_user_name=$(id -un 2>/dev/null) && [[ -n $job_user_name ]]; then
        USER=$job_user_name
    else
        USER="uid$(id -u)"
    fi
fi
export USER
export LOGNAME=${LOGNAME:-$USER}
