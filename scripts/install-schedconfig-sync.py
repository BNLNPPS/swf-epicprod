#!/usr/bin/env python3
"""Install the approved CRIC plus GitHub override policy on a production host.

Run as root from an extracted swf-epicprod checkout: HOST is pandaserver01,
pandaharvester01 or osgsub01. Keeps dated backups, writes the cron job,
and on OSG also applies configure-osg-regulation.py and restarts Harvester.
See docs/REGULATION_AUDIT_2026-10-22.md for activation and rollback.
"""
import datetime, json, os, pathlib, pwd, shutil, subprocess, sys
if len(sys.argv) != 2 or sys.argv[1] not in ('pandaserver01', 'pandaharvester01', 'osgsub01'):
    sys.exit(__doc__)
host = sys.argv[1]
base = pathlib.Path({'pandaserver01':'/opt/panda/etc/panda/schedconfig',
                     'pandaharvester01':'/data/atlpan/harvester_common/schedconfig',
                     'osgsub01':'/var/data/atlpan/harvester_common/schedconfig'}[host])
stamp = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
account = pwd.getpwnam('atlpan')
def backup(path):
    if path.exists(): shutil.copy2(str(path), str(path)+'.before-regulation-'+stamp)
base.mkdir(parents=True, exist_ok=True)
os.chown(str(base),account.pw_uid,account.pw_gid)
src=pathlib.Path(__file__).resolve().parents[1]
for relative in ('scripts/panda-schedconfig.py','pandaserver/schedconfig-overrides.json'):
    dest=base/pathlib.Path(relative).name
    backup(dest); shutil.copyfile(str(src/relative),str(dest)); os.chown(str(dest),account.pw_uid,account.pw_gid)
    os.chmod(str(dest),0o644)
# This identifies the installed writer; the sidecar identifies each synchronized input.
try:
    revision=subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD'],universal_newlines=True).strip()
except (OSError, subprocess.CalledProcessError):
    revision='archive; see schedconfig.json.provenance.json for synchronized input revision'
(base/'COMMIT').write_text(revision+'\n')
args=['/usr/bin/python3',str(base/'panda-schedconfig.py'),str(base/'schedconfig-overrides.json'),str(base/'schedconfig.json'),'--sync-from-github']
if host=='pandaharvester01': args+=['--pilot-dir',str(base.parent/'queuedata')]
subprocess.run(['runuser','-u','atlpan','--']+args, check=True)
cron=pathlib.Path('/etc/cron.d/'+('panda-schedconfig' if host=='pandaserver01' else 'harvester-schedconfig'))
backup(cron)
cron.write_text('# CRIC plus revision-pinned GitHub overrides; last-good data survives source outages.\n'
                '*/5 * * * * atlpan /usr/bin/flock -n '+str(base/'.writer.lock')+' '+' '.join(args)+' >> '+str(base/'schedconfig.log')+' 2>&1\n')
os.chmod(str(cron),0o644)
if host=='osgsub01':
    cfg=pathlib.Path('/opt/harvester/etc/panda/panda_harvester.cfg')
    text=cfg.read_text()
    lines=text.splitlines(True)
    found=0
    for i,line in enumerate(lines):
        if line.lstrip().startswith('panda_queues.json||'):
            lines[i]=' panda_queues.json||file:'+str(base/'schedconfig.json')+'\n';found+=1
    assert found==1, 'expected one queue cache source'
    backup(cfg);cfg.write_text(''.join(lines))
    subprocess.run(['/opt/harvester/bin/python',str(src/'scripts/configure-osg-regulation.py'),'--apply'],check=True)
    subprocess.run(['systemctl','stop','panda_harvester-uwsgi'],check=True)
    try:
        command='set -e; set -a; source /opt/harvester/etc/sysconfig/panda_harvester_env; set +a; /opt/harvester/bin/python -m pandaharvester.harvesterscripts.harvester_admin qconf refresh'
        subprocess.run(['runuser','-u','atlpan','--','bash','-c',command],check=True)
    finally:
        subprocess.run(['systemctl','start','panda_harvester-uwsgi'],check=True)
print('INSTALLED',host,json.loads((base/'schedconfig.json.provenance.json').read_text()))
