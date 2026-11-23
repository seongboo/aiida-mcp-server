# aiida-mcp-server

Read-only MCP server that lets Claude query an AiiDA profile.

## Tools
| Tool | Purpose |
|---|---|
| `list_processes(state, process_label, limit)` | Recent processes, newest first |
| `get_node(pk)` | Compact node summary (no large arrays) |
| `query_structures(formula, elements, limit)` | Search StructureData by reduced formula / elements |

No tool writes to the database.

## Install (on the machine that holds your AiiDA profile)
```bash
git clone https://github.com/seongboo/aiida-mcp-server.git && cd aiida-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Connect to Claude Desktop
Add to `claude_desktop_config.json` (use absolute paths; `AIIDA_PROFILE` is optional):
```json
{
  "mcpServers": {
    "aiida": {
      "command": "/ABS/PATH/aiida-mcp-server/.venv/bin/aiida-mcp-server",
      "env": { "AIIDA_PROFILE": "your-profile-name" }
    }
  }
}
```
Restart Claude Desktop afterwards.

## Notes
- `query_structures` matches the reduced formula in Python over the 5000 newest StructureData nodes.
- Tested against aiida-core 2.9 and mcp 2.3.
