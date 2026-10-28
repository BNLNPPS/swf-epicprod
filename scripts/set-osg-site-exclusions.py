#!/usr/bin/env python3
"""Set ePIC's production OSG pilots' undesired sites and lift exact-site exclusions.

Run from the operating account on swf-testbed. The submit description on
osgsub01 is backed up, its `+UNDESIRED_Sites` line is set to the given list,
and each named exact-site requirement (`TARGET.GLIDEIN_Site =!= "SITE"`, the
clause exclude-osg-site.py adds) is removed. Idle BNL_OSG_PanDA_1 pilots
receive the same changes; running workers are never edited or removed.
Without --apply the plan is reported and nothing changes.
"""
import argparse
import re
import shlex
import subprocess
import sys


def site_clause(site):
    """The appended exact-site exclusion, in either spelling of ClassAd's =!=."""
    return (r'\s*&&\s*\(\s*TARGET\.GLIDEIN_Site\s*(?:=!=|isnt)\s*"'
            + re.escape(site) + r'"\s*\)')


def excludes(expression, site):
    return re.search(r'\bTARGET\.GLIDEIN_Site\s*(?:=!=|isnt)\s*"'
                     + re.escape(site) + '"', expression) is not None


def lift(expression, sites):
    """Remove each site's exclusion; the parentheses it leaves stay balanced."""
    for site in sites:
        expression = re.sub(site_clause(site), '', expression)
    if expression.count('(') != expression.count(')'):
        raise ValueError('unbalanced Requirements after lifting')
    return expression


def requirements_value(value):
    """Condor JSON marks non-literal expressions as /Expr(expression)/."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing Requirements expression')
    if value.startswith('/Expr(') and value.endswith(')/'):
        return value[6:-2]
    return value


def rewrite(text, undesired, sites):
    """The description with the undesired list set and the sites lifted."""
    lines = text.splitlines(keepends=True)
    req = [i for i, line in enumerate(lines) if re.match(r'^Requirements\s*=', line, re.I)]
    und = [i for i, line in enumerate(lines) if re.match(r'^\+UNDESIRED_Sites\s*=', line)]
    if len(req) != 1 or len(und) != 1:
        raise ValueError('expected one Requirements and one +UNDESIRED_Sites line')
    lines[req[0]] = lift(lines[req[0]].rstrip('\n'), sites) + '\n'
    lines[und[0]] = '+UNDESIRED_Sites = "' + undesired + '"\n'
    return ''.join(lines)


REMOTE = r'''
import datetime,fcntl,json,os,shutil,subprocess,sys,tempfile
undesired,sites,apply=sys.argv[1],[s for s in sys.argv[2].split(',') if s],sys.argv[3]=='apply'
path='/var/data/atlpan/harvester_common/submit_pilot2_pull_bnl_osg.sdf'
base=('Owner == "atlpan" && harvesterID == "BNL_osg_harvester_1" && '
      'regexp("(^| )-q BNL_OSG_PanDA_1( |$)", Args) && JobStatus == 1')
def idle():
    return json.loads(subprocess.check_output(
        ['condor_q','-allusers','-constraint',base,'-json','-attributes',
         'ClusterId,ProcId,Requirements,UNDESIRED_Sites'],universal_newlines=True,timeout=60) or '[]')
def stale(ad):
    r=requirements_value(ad.get('Requirements'))
    return any(excludes(r,s) for s in sites) or ad.get('UNDESIRED_Sites')!=undesired
with open(path+'.site-exclusion.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    with open(path) as f:before=f.read()
    after=rewrite(before,undesired,sites)
    ads=idle()
    pending=[a for a in ads if stale(a)]
    result={'queue':'BNL_OSG_PanDA_1','template':path,'undesired':undesired,'lifted':sites,
            'template_change':before!=after,'idle':len(ads),'idle_to_update':len(pending),
            'applied':apply,'running_workers_edited':0}
    if apply:
        if before!=after:
            backup=path+'.before-site-exclusions-'+datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
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
        subprocess.check_output(['condor_qedit','-constraint',base,'UNDESIRED_Sites','"'+undesired+'"'],
                                universal_newlines=True,stderr=subprocess.STDOUT,timeout=60)
        updated=0
        for ad in pending:
            old=requirements_value(ad.get('Requirements'))
            new=lift(old,sites)
            if new==old:continue
            job='%s.%s'%(ad['ClusterId'],ad['ProcId'])
            out=subprocess.check_output(['condor_qedit','-jobids',job,'-constraint',base,'Requirements',new],
                                        universal_newlines=True,stderr=subprocess.STDOUT,timeout=30)
            if 'for 1 matching jobs' in out:updated+=1
        remaining=idle()
        left=[a for a in remaining if stale(a)]
        if left:raise RuntimeError('%d idle pilots still carry a lifted site or the old undesired list'%len(left))
        with open(path) as f:now=f.read()
        if any(excludes(now,s) for s in sites) or ('+UNDESIRED_Sites = "'+undesired+'"') not in now:
            raise RuntimeError('template change not retained')
        result.update(idle_requirements_updated=updated,idle_verified=len(remaining),
                      applied_at=datetime.datetime.utcnow().isoformat()+'Z')
    print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--undesired', required=True,
                        help='the complete +UNDESIRED_Sites list, e.g. "GREX, Alabama-CHPC"')
    parser.add_argument('--lift', action='append', default=[],
                        help='an exact-site exclusion to remove (repeatable)')
    parser.add_argument('--reason', required=True, help='the decision and evidence')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+(, [A-Za-z0-9_.-]+)*', args.undesired):
        parser.error('undesired list must be "SITE, SITE, ..."')
    for site in args.lift:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', site):
            parser.error('invalid exact GLIDEIN_Site name: ' + site)
    import inspect
    code = ('import re\n' + ''.join(inspect.getsource(f) + '\n' for f in
                                    (site_clause, excludes, lift, requirements_value, rewrite))
            + REMOTE)
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'osgsub01',
         ' '.join(shlex.quote(a) for a in
                  ('sudo', '-n', 'python3', '-', args.undesired, ','.join(args.lift),
                   'apply' if args.apply else 'plan'))],
        input=code, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=900)
    if result.returncode:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    print(result.stdout.strip())
    print('Reason: ' + args.reason)


if __name__ == '__main__':
    main()
