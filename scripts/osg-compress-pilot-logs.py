#!/usr/bin/env python3
"""Compress finished pilot logs on osgsub01 to keep /var from filling.

Run from the operating account on swf-testbed. Harvester's pilot stdout,
stderr and condor user logs collect under /var/data/atlpan/condor_logs in
one directory per submission hour (YY-MM-DD_HH), and nothing on the host
rotates them. This gzips the .out, .err and .log files in those hour
directories that have not changed for --idle-hours and belong to no job
still in the condor queue (idle or running), so no live pilot's file is
touched. Named directories (trial and cohort evidence) are left alone.
gzip keeps owner, mode and time; gunzip restores a file. A compressed log
is served as NAME.gz, so a link to NAME no longer resolves.

Without --apply it reports what would be compressed. With --apply it
starts the compression detached on the host at the lowest CPU and I/O
priority and returns at once; the file list and log are kept beside each
other under /var/log/epicprod-pilot-log-gzip/.
"""
import argparse
import subprocess
import sys

REMOTE = r'''
import json,os,re,subprocess,sys,time
idle_h,apply=float(sys.argv[1]),sys.argv[2]=='apply'
root='/var/data/atlpan/condor_logs'
hour=re.compile(r'^\d{2}-\d{2}-\d{2}_\d{2}$')
out=subprocess.check_output(['condor_q','-allusers','-af','UserLog','Out','Err'],
                            universal_newlines=True,timeout=120)
live=set()
for line in out.splitlines():
    for p in line.split():
        if p and p!='undefined':live.add(os.path.realpath(p))
cut=time.time()-idle_h*3600
files=[];size=0;skipped_live=0;skipped_recent=0
for d in sorted(os.listdir(root)):
    if not hour.match(d):continue
    base=os.path.join(root,d)
    for dirpath,_,names in os.walk(base):
        for n in names:
            if not n.endswith(('.out','.err','.log')):continue
            p=os.path.join(dirpath,n)
            try:st=os.lstat(p)
            except OSError:continue
            if not os.path.isfile(p) or os.path.islink(p):continue
            if os.path.realpath(p) in live:skipped_live+=1;continue
            if st.st_mtime>cut:skipped_recent+=1;continue
            files.append(p);size+=st.st_size
st=os.statvfs('/var')
result={'files':len(files),'bytes':size,'skipped_live':skipped_live,'skipped_recent':skipped_recent,
        'live_paths':len(live),'var_free_gb':round(st.f_bavail*st.f_frsize/1e9,1),'applied':apply}
if apply and files:
    work='/var/log/epicprod-pilot-log-gzip';os.makedirs(work,exist_ok=True)
    tag=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
    lst=os.path.join(work,tag+'.list');log=os.path.join(work,tag+'.log')
    with open(lst,'wb') as f:f.write(b'\0'.join(p.encode() for p in files)+b'\0')
    cmd=('nice -n 19 ionice -c3 xargs -0 -a %s -P 2 -n 200 gzip -- >> %s 2>&1; echo "done $(date -u +%%FT%%TZ)" >> %s'
         %(lst,log,log))
    p=subprocess.Popen(['setsid','bash','-c',cmd],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,close_fds=True)
    result.update(list=lst,log=log,pid=p.pid)
print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--idle-hours', type=float, default=6.0,
                        help='compress only files unchanged for this long (default 6)')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.idle_hours < 1:
        parser.error('--idle-hours must be at least 1')
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'osgsub01',
         'sudo -n python3 - %g %s' % (args.idle_hours, 'apply' if args.apply else 'plan')],
        input=REMOTE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=1200)
    if result.returncode:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    print(result.stdout.strip())


if __name__ == '__main__':
    main()
