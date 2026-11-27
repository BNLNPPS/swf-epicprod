# Segfault delegation

A crash in the segfault catalog ([SEGFAULT_DIAGNOSIS.md](SEGFAULT_DIAGNOSIS.md))
can be delegated to a person or a physics working group outside the
production operations team. The request, as Torre Wenaus recorded it on
2026-10-27: "a delegation mechanism (as in delegate to a person or physics
working group who is not the ops team) in the segfault analysis system".
Stages 1 and 2 of the build below are in place (the record, the MCP tools,
REST and the pages); stage 3, the notice events, is not yet built.

## The delegation

The unit is a finding, the curated reading of one crash
(SEGFAULT_DIAGNOSIS.md, Findings), since the findings are where the
handoffs already live. A crash that no finding reads gets a finding before
it is delegated; a short one stating the signature and the ask is enough.

The record is kept in the finding's data under `delegations`, a list in
order of creation, versioned with the finding:

- `delegate`: a person (an epicprod account name) or a group (a name in the
  group registry below);
- `ask`: what is asked, in one or two sentences, such as reading the crash,
  fixing the software, or deciding whether the stated loss is acceptable;
- `by` and `at`: the operator who delegated and when;
- `state`: `delegated`, `accepted`, `answered` or `returned`;
- `answer`: the delegate's reply, with its author and time.

A finding can carry several delegations at once, for example to a physics
working group and to a software maintainer. A delegation is open until it
is answered or returned.

## Groups

The delegate groups are a registry held as data, the SysConfig key
`segfault_delegate_groups`, edited on the System page. Each group has a
name, a one-line scope, and its conveners and members by epicprod account
name. The expected entries are the physics working groups and the software
groups the findings hand off to (npsim and DD4hep, EICrecon); the list is
the production lead's to set.

## Who acts

Delegating is an authenticated operator decision (EPICPROD_OPS.md,
Operational control tiers). It is made from the finding's row on the
findings page, from the signature page, with the MCP tool
`panda_segfault_delegate`, or with
`POST /api/segfaults/findings/<name>/delegations/`, and each delegation is
a `segfault_delegated` action. An LLM session does not delegate on its own;
its reading may recommend a delegation. The delegate, or a member of the
delegated group, accepts, answers or returns the delegation from the same
places (the MCP tool `panda_segfault_delegation_reply`, or
`POST /api/segfaults/findings/<name>/delegations/<id>/` with `state` and
`text`), as a `segfault_delegation_answered` action. A reply that answers
or returns carries its text; an answered or returned delegation takes no
further reply. `panda_segfault_delegations` and
`GET /api/segfaults/delegations/?person=<account>&open=1` list the
delegations, open ones first, with the group registry.

As built: `delegate_finding`, `reply_delegation`, `delegations` and
`delegate_groups` in swf-monitor `monitor_app/segfaults.py`, writing through
`set_finding` so each change is a version of the finding. A signed-in caller
is recorded as themselves; a service token names who acts. The two write
tools are under the authority gate (swf-monitor AUTHORITY.md).

## What the delegate sees

- The findings page carries a Delegated column (the open delegations and
  their state) and, under each finding, its delegations with their replies
  and, for an account that may act, a Delegate control.
- The catalog carries a Delegated column and a Delegated facet in its
  narrowing filter (open, answered, returned, not delegated), read through
  each signature's finding.
- The Delegated to me tab, `/panda/segfaults/delegated/`, lists the
  delegations to the signed-in account and to the groups that list it,
  open ones first (`?all=1` lists every delegation). Each row carries the
  finding, the ask, its crashes and loss, and the replies, with Accept,
  Answer and Return for the delegate.

The page actions are JSON POSTs under `panda/segfaults/`
(`findings/<name>/delegate/` and
`findings/<name>/delegations/<id>/reply/`), so they work through the
swf-remote proxy (swf-monitor EXTERNAL_ACCESS.md).

## Notices

`segfault_delegated` and `segfault_delegation_answered` are action-stream
events. Notice routing (swf-monitor NOTICE_ROUTING.md) delivers them to
subscribers who registered for them, with a filter on the delegate.
Nothing sends mail or writes to a person who has not subscribed, in keeping
with the production disposition (SEGFAULT_DIAGNOSIS.md): no machinery files
an issue or writes to a site.

## Effect on production

A delegation records who is asked and what they answered. It changes no
production disposition: holding a configuration, a residual rerun, or
accepting the loss remain operator decisions on the record.

## Build

The record is entry-store data on the finding and the registry is a
SysConfig key, so no schema change is needed. The stages, each deployed and
checked on the external face before the next:

1. The record, the MCP tool and REST.
2. The findings-page column and facet, and the Delegated to me page.
3. The notice events.

## Open questions

- The initial group list.
