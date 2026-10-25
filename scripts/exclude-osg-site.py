#!/usr/bin/env python3
"""Exclude one failing glidein site from ePIC's production OSG pilots.

Run from the operating account on swf-testbed. The submit description is
backed up before its exact-site requirement is added; existing idle ads
receive the same requirement without changing their other constraints.
Running workers are never edited or removed. See EPICPROD_WATCHER_BRIEF.md
for the standing operational authority and required failure evidence.
"""
import argparse
import re
import subprocess
import sys


def excludes(expression, site):
    """Recognize both spellings of ClassAd's exact inequality operator."""
    return re.search(r'\bTARGET\.GLIDEIN_Site\s*(?:=!=|isnt)\s*"'
                     + re.escape(site) + '"', expression) is not None


def requirements_value(value):
    """Condor JSON marks non-literal expressions as /Expr(expression)/."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing Requirements expression')
    if value.startswith('/Expr(') and value.endswith(')/'):
        return value[6:-2]
    return value


def exclude(text, site):
    """Preserve the description and append an exact-site requirement once."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', site):
        raise ValueError('invalid exact GLIDEIN_Site name')
    clause = 'TARGET.GLIDEIN_Site =!= "' + site + '"'
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines)
            if re.match(r'^Requirements\s*=', line, re.I)]
    if len(hits) != 1:
        raise ValueError('expected one Requirements line')
    i = hits[0]
    if not excludes(lines[i], site):
        before = lines[i].split('=', 1)[1].strip()
        lines[i] = 'Requirements = (' + before + ') && (' + clause + ')\n'
    return ''.join(lines)


REMOTE = r'''
import datetime,fcntl,json,os,shutil,subprocess,sys,tempfile
site,apply=sys.argv[1],sys.argv[2]=='apply'
path='/var/data/atlpan/harvester_common/submit_pilot2_pull_bnl_osg.sdf'
clause='TARGET.GLIDEIN_Site =!= "'+site+'"'
base=('Owner == "atlpan" && harvesterID == "BNL_osg_harvester_1" && '
      'regexp("(^| )-q BNL_OSG_PanDA_1( |$)", Args) && JobStatus == 1')
def idle():
    return json.loads(subprocess.check_output(
        ['condor_q','-allusers','-constraint',base,'-json','-attributes',
         'ClusterId,ProcId,Requirements'],universal_newlines=True,timeout=30))
with open(path+'.site-exclusion.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    with open(path) as f:before=f.read()
    after=exclude(before,site)
    ads=idle()
    pending=[a for a in ads if not excludes(a.get('Requirements',''),site)]
    result={'site':site,'queue':'BNL_OSG_PanDA_1','template':path,
            'template_change':before!=after,'idle_to_update':len(pending),
            'applied':apply,'running_workers_edited':0}
    if apply:
        if before!=after:
            backup=path+'.before-exclude-'+site+'-'+datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
            shutil.copy2(path,backup)
            info=os.stat(path)
            fd,tmp=tempfile.mkstemp(dir=os.path.dirname(path),suffix='.sdf')
            try:
                with os.fdopen(fd,'w') as f:f.write(after)
                os.chmod(tmp,info.st_mode & 0o7777)
                os.chown(tmp,info.st_uid,info.st_gid)
                with open(path) as f:
                    if f.read()!=before:raise RuntimeError('submit description changed concurrently')
                os.replace(tmp,path)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            result['backup']=backup
        updated=0
        for ad in pending:
            job='%s.%s'%(ad['ClusterId'],ad['ProcId'])
            old=requirements_value(ad.get('Requirements'))
            out=subprocess.check_output(
                ['condor_qedit','-jobids',job,'-constraint',base,
                 'Requirements','('+old+') && ('+clause+')'],
                universal_newlines=True,stderr=subprocess.STDOUT,timeout=30)
            if 'for 1 matching jobs' in out:updated+=1
        remaining=idle()
        missing=[a for a in remaining if not excludes(a.get('Requirements',''),site)]
        if missing:raise RuntimeError('%d idle pilots still lack the exclusion'%len(missing))
        with open(path) as f:
            if clause not in f.read():raise RuntimeError('template exclusion not retained')
        result.update(idle_updated=updated,idle_verified=len(remaining),
                      applied_at=datetime.datetime.utcnow().isoformat()+'Z')
    print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--reason', required=True,
                        help='the evidence recorded with the operational outcome')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    exclude('Requirements = True\n', args.site)
    import inspect
    code = ('import re\n' + inspect.getsource(excludes) + '\n'
            + inspect.getsource(requirements_value) + '\n'
            + inspect.getsource(exclude) + '\n' + REMOTE)
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'osgsub01',
         'sudo', '-n', 'python3', '-', args.site, 'apply' if args.apply else 'plan'],
        input=code, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=240)
    if result.returncode:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    print(result.stdout.strip())
    print('Evidence: ' + args.reason)


if __name__ == '__main__':
    main()
