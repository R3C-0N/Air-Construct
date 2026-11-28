"""Let Claude (or any MCP client) drive the open Air-Construct window.

Owned by: workstream "assistant".

Two processes talk to each other:

- the desktop app runs a small *bridge* (``airconstruct.ui.assistant``): a TCP server
  on 127.0.0.1 that executes commands in the window the user is looking at, so every
  action shows on screen as it happens (tab switch, selection, undo step named
  "Claude · ...", activity pill in the top bar);
- ``airconstruct-mcp`` (:mod:`airconstruct.assistant.mcp_server`) is the MCP server
  Claude Desktop / Claude Code starts over stdio. It turns MCP tool calls into bridge
  commands, and starts the app when it is not running.

They find each other through a small JSON file (:mod:`airconstruct.assistant.protocol`)
holding the port and a random token, readable only by the user.
"""
