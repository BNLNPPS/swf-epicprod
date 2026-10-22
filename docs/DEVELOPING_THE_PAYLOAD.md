# Developing the payload

The epicprod payload (`swf_epicprod/payload`, [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md))
is the code every production job runs. It reaches production on merge:
`main` deploys itself ([AUTODEPLOY.md](AUTODEPLOY.md)), and each
submission copies the payload from the deployed release into the task's
sandbox. A change merged to `main` therefore runs in every production
job submitted after the deploy.

## Switches

New behaviour enters the payload behind a switch: an environment
variable named `EPICPROD_SWITCH_<NAME>`, read where the behaviour
lives, the behaviour off when the variable is unset. A merged change
whose switch is unset leaves production as it was. When the change is
proven, a follow-up pull request turns it on by default or removes the
switch.

## Testing a change with a payload canary

A payload canary (site-canary
[IMPLEMENTATION.md](https://github.com/BNLNPPS/site-canary/blob/main/docs/IMPLEMENTATION.md),
Payload canaries) runs the payload on one manifest row of a chosen PCS
task, as a one-job canary task on a chosen queue, and returns a
checklist verdict read from the payload's own report: input,
simulation, reconstruction, validation, registration, the payload exit
code, events produced against those requested, and the output
available in the catalog. Its outputs go to `epic:/TEST/canary/<stamp>`
with a seven-day lifetime; no production path or record is touched.

The Run control on the canary page (`/prod/canary/`) takes, beside the
task and the queue:

- **Payload source**: the deployed payload by default, or the payload
  of a pull request (`pr:<N>`), a branch (`<branch>`), or a fork's
  branch (`<owner>:<branch>`). The submission exports
  `swf_epicprod/payload` at that commit into the sandbox. A pull
  request from a fork needs no write access to the repository.
- **Switches**: `EPICPROD_SWITCH_<NAME>=<value>` settings for the job,
  one per line. Values carry no spaces or shell characters.
- **Credential**: `x509` (default), the eicprod proxy shipped as
  production ships it; or `token`, which ships no proxy and ships the
  token file configured for canaries as `BEARER_TOKEN_FILE`, so a
  token path is proven on its own. A token canary is refused, with that
  reason, while no token file is configured. Or `panda`, which ships no
  credential and empties `X509_USER_PROXY`, so the job writes and
  registers only with the tokens it gets from the PanDA server (item 22
  of EPICPROD_PAYLOAD.md); a proxy fallback has nothing to fall back to,
  and the canary fails where a token does not come.
- **Threads** and **container image**, as for any payload canary.

The run records the payload source, the commit it ran and who sent
it, because the code runs with the production credential. Sending a
canary needs a sign-in at epic-devcloud.org through GitHub as a member
of the eic organisation.

## The sequence

1. Put the change behind its switch, off by default.
2. Open the pull request and run payload canaries from it, with the
   switch set, on the task and queue the change concerns, until the
   verdict is clean at the head commit.
3. Merge. Production is unchanged.
4. Turn the switch on in a follow-up pull request, proven the same way.

A payload pull request is to carry a passing canary at its head commit,
reported to the pull request as a check, before it merges; that check
is not built yet.
