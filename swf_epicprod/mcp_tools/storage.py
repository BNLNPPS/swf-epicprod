"""MCP retrieval for the storage record's exception listings
(docs/STORAGE.md, Retrieval): the full ghost, stuck-rule and
stalled-dataset lists the storage component carries only the head of,
served from the pass's store."""

from asgiref.sync import sync_to_async

from monitor_app.mcp import mcp


@mcp.tool()
async def epicprod_storage(listing: str = 'ghosts', rse: str = '',
                           campaign: str = '', state: str = '',
                           limit: int = 100, offset: int = 0) -> dict:
    """Storage exception listings from the production storage record.

    The catalog account of what is wrong with placed production data on
    the JLab RSEs, served from the storage pass's store as it stands.
    Use it for "which ghost DIDs are at RSE X", "is the ghost list
    growing", "which rules are stuck", "which datasets stopped
    arriving". For placement counts over time use the Snapper tools on
    the storage component instead.

    Args:
        listing: 'ghosts' (default), 'stuck_rules' or 'stalled_datasets'.
            A ghost is a registered file with no AVAILABLE replica on any
            RSE, held by its replicas in other states (COPYING and so on)
            or by the pseudo-RSE 'none' when it has no replica row at all.
        rse: restrict to one holding RSE (e.g. 'ASGC-XRD'); 'none' for
            ghosts with no replica row at all.
        campaign: restrict to one campaign family (e.g. '26.07').
        state: restrict ghosts to one holding replica state or stuck
            rules to one rule state.
        limit: rows per page, 1 to 1000 (default 100).
        offset: page start.

    Returns:
        A document with 'as_of' (the last completed pass and any pass in
        progress, so a reader knows how far the record reaches),
        'total', 'rows' oldest first, 'next_offset' when more follow, and
        for ghosts 'by_rse': the account of the filtered population by
        holding RSE, with files, bytes, by_state, by_campaign and the
        oldest entry. A store that cannot be read returns 'error'.
    """
    from swf_epicprod.analytics.storage_listings import listing as _listing

    return await sync_to_async(_listing)(
        listing, rse=rse, campaign=campaign, state=state,
        limit=limit, offset=offset)
