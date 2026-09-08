# molecule-lab-contributor-agent

The plugin itself. Everything explaining what it does, how to install it, and how to
configure it lives in the [repository README](../../README.md) one level up, along with the
Apache-2.0 [LICENSE](../../LICENSE).

You are in the right directory if you want to point a host at the plugin by hand:

```bash
claude --plugin-dir plugins/molecule-lab-contributor-agent      # from the checkout root
```

```
.claude-plugin/plugin.json   the manifest Claude Code and Grok read
.codex-plugin/plugin.json    the same, in Codex's format
.mcp.json                    the mol-labs server entry — the only copy; neither manifest repeats it
.env.example                 a template. Do NOT fill it in here; see Configuration in the repo README
hooks/hooks.json             SessionStart: installs a private uv and the server's Python, once
mcp/launch, mcp/launch.cmd   what the host actually starts; they bootstrap, then exec the server
mcp/server.py                the server. Its dependencies are declared inline (PEP 723)
skills/                      the playbook the driving agent follows
```

Created at runtime and gitignored: `.plugin-data/` (the private uv, Python, and the `.env`
the server manages) and `mcp/__pycache__/`.
