import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
role = sys.argv[1]
evidence = Path(sys.argv[2])
evidence.write_text('started\n')
server = FastMCP(role)
@server.tool()
def worker_probe() -> str:
    """Return the fixed marker verifying this approved connector is reachable."""
    (evidence.parent/(role+'-called')).write_text('called\n')
    return 'MCP_SERVER_SHARED_OK'
@server.tool()
def ungranted_probe() -> str:
    """This probe is excluded from the granted tool allowlist and must be unavailable."""
    (evidence.parent/'ungranted-called').write_text('called\n')
    return 'UNGRANTED_TOOL_CALLED'
@server.resource('fixture://scope-marker')
def scope_marker() -> str:
    """Return a harmless resource marker for the connector scope capture."""
    (evidence.parent/'resource-called').write_text('called\n')
    return 'MCP_RESOURCE_READ_OK'
server.run(transport='stdio')
