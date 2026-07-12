"""epicprod MCP tools, registered on the platform MCP service.

There is one FastMCP instance (``monitor_app.mcp.mcp``) and one MCP
service downstream; this module registers domain tools on it, and the
monitor's ``mcp/__init__`` imports this module as its registration shim
(ARCHITECTURE_MAP.md: tool modules follow their domain, the runtime is
platform).
"""

from asgiref.sync import sync_to_async

from monitor_app.mcp import mcp


@mcp.tool()
async def epicprod_campaign_status(campaign: str = '', window_days: float = 1) -> dict:
    """Production campaign status rollup — the assessment evidence document.

    Returns one JSON document for a campaign: analytics member blocks
    (campaign_progress, panda_health, rucio_arrivals, disposition_mix,
    action_stream_activity, credential_status), the mechanical verdict
    floor {verdict: ok|attention|alarm, reasons}, the current assessment
    target list, and the enable gate. Every number a campaign assessment
    may state must come from this document; the floor is the minimum
    verdict — raise it with justification if warranted, never lower it.

    Args:
        campaign: campaign name (e.g. '26.06.0'). Default: the first
            producing campaign, else the current campaign.
        window_days: activity window for deltas, disposition flips, and
            action aggregation. Use 1 for a nightly assessment, 7 for a
            weekly.

    A member with data.available=false could not read its source; treat
    that as missing evidence worth reporting, not an error to hide.
    """
    from pcs.services import ServiceError
    from swf_epicprod.analytics.rollup import campaign_status

    try:
        return await sync_to_async(campaign_status)(
            campaign or None, window_days=window_days)
    except ServiceError as e:
        return {'error': str(e)}
