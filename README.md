# NIRA Process Forge MCP

> Process management for Claude — launch, monitor, and control background system processes

[![PyPI version](https://badge.fury.io/py/nira-process-forge.svg)](https://pypi.org/project/nira-process-forge/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Quick Start

```bash
pip install nira-process-forge
```

Add to your Claude Code MCP config (`~/.claude.json`):
```json
{
  "mcpServers": {
    "nira-process-forge": {
      "command": "python",
      "args": ["-m", "nira_process_forge"]
    }
  }
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `PROC_FORGE_LOG_DIR` | Optional | Directory for process logs (default: ~/proc_forge_logs) |

## License

MIT — built by [pachote](https://github.com/pachote)
