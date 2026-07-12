"""epicprod MCP tools, registered on the platform MCP service.

There is one FastMCP instance (``monitor_app.mcp.mcp``) and one MCP
service downstream; these modules register domain tools on it, and the
monitor's ``mcp/__init__`` imports this package as its registration
shim (ARCHITECTURE_MAP.md: tool modules follow their domain, the
runtime is platform).
"""

from .status import epicprod_campaign_status
from .actions import epicprod_list_actions
from .proposals import (
    ai_list_proposals,
    ai_decide_proposal,
)
from .pcs import (
    pcs_list_tags,
    pcs_get_tag,
    pcs_search_tags,
    pcs_dataset_list,
    pcs_dataset_get,
    pcs_dataset_intake,
    pcs_prodtask_list,
    pcs_prodtask_get,
    pcs_prodtask_artifact,
    pcs_prodtask_intake,
    pcs_prodtask_link_input,
    pcs_prodtask_set_status,
)

# Assessment subject types register on the platform ai_content mechanism
# when the domain tools load.
from swf_epicprod import ai_subjects  # noqa: F401  (registration side effect)

__all__ = [
    'epicprod_campaign_status',
    'epicprod_list_actions',
    'ai_list_proposals',
    'ai_decide_proposal',
    'pcs_list_tags',
    'pcs_get_tag',
    'pcs_search_tags',
    'pcs_dataset_list',
    'pcs_dataset_get',
    'pcs_dataset_intake',
    'pcs_prodtask_list',
    'pcs_prodtask_get',
    'pcs_prodtask_artifact',
    'pcs_prodtask_intake',
    'pcs_prodtask_link_input',
    'pcs_prodtask_set_status',
]
