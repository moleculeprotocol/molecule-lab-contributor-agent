# Molecule Labs — agent skill

A skill plus a small MCP server that let an AI agent write files into a **Molecule Lab that
a human owns**.

The researcher makes their Lab in the [Labs app](https://labs.molecule.xyz) — email sign-in,
no wallet, no code. Their agent then gets its **own** identity rather than borrowing theirs:

| # | Actor | Action |
|---|-------|--------|
| 1 | Agent | Creates a wallet and reports the address |
| 2 | **Human** | Adds that address to their Lab as **Contributor** |
| 3 | Agent | Issues its own write token |
| 4 | Agent + **Human** | Agrees what is being uploaded, where, and whether it is public or private |
| 5 | Agent | Uploads — plaintext, or encrypted |
| 6 | Agent | Verifies it, and hands back a link |

**The human never hands over a private key, a token, or their session.** Revoking the agent
is one click that touches nothing else.

## The agent asks before it publishes

Publishing to a data room is a one-way door: a public file is downloadable by anyone with
the link the moment it lands, a path can never be reused, and nothing un-publishes what was
already fetched.

So **the agent never picks the visibility.** Step 4 is not advisory — the server stages every
upload, hands the plan back to be read out, and refuses to write anything until the human's
actual answer has been recorded. Placeholder approvals are rejected.

Two things the skill is loud about, because both quietly publish confidential data:

- The access level is a **label, not a lock**. Uploading plaintext and marking it
  confidential succeeds and stores your plaintext. Only encrypting encrypts.
- A private file's ciphertext is still downloadable by anyone who can query the Lab. The
  confidentiality is in the encryption, not the label.

## Public and private uploads

| | |
|---|---|
| **Public** | plaintext; anyone with the link can download it, permanently. |
| **Private** | AES-256-GCM encrypted before it leaves the machine. The key is released only to wallets the Lab's on-chain access conditions admit — by default its Contributors and its owner. |

The encryption is the same envelope the Labs app uses, so a file the agent encrypts opens in
the app. It is strong and rule-based but **not** zero-knowledge: Molecule operates the key
service, so Molecule's infrastructure can decrypt the file. The skill says so rather than
overselling it.

Note that the default is one notch tighter than the app: read-only **Viewers** cannot open a
file the agent encrypts unless you ask for `conditionRole="viewer"`.

## What it costs

Nothing, on either path. The agent's key signs exactly one off-chain message and never sends
a transaction — no gas, no tokens, no payments. Encryption adds no cost.

## What you need

| | |
|---|---|
| **A Molecule API credential** | The `mol_…` string. Think of it as an API key — it says whose calls these are and can be revoked. Starter-pack users have one; otherwise ask on the [Molecule Discord](https://t.co/L0VEiy4Bjk). |
| **Your Lab** | Just the URL from its page. Nobody needs to find its 32-byte id. |

That is the whole list. There is nothing to install by hand: no Python, no `pip`, no
virtualenv, no uv. The plugin sets itself up the first time it runs (see below).

## Install

Paste these two lines into the Claude Code prompt, one at a time — in the Claude desktop
app's **Code** tab as much as in a terminal. No terminal is needed.

```
/plugin marketplace add moleculeprotocol/molecule-lab-contributor-agent
/plugin install molecule-lab-contributor-agent@molecule-lab-contributor-agent-marketplace
```

Driving it with something other than Claude — Grok, or any MCP client — is supported and
takes about as long: see [Running it on another agent](#running-it-on-another-agent).

The first session after installing spends its first half-minute or so setting up, with a
status line that says so, and the `mol-labs` tools are ready in that same session. On a slow
connection the first attempt can time out; the agent will then tell you to type
`/reload-plugins` once. Every session after that starts instantly.

Or from a clone:

```bash
git clone https://github.com/moleculeprotocol/molecule-lab-contributor-agent.git
claude --plugin-dir /path/to/molecule-lab-contributor-agent/plugins/molecule-lab-contributor-agent
```

The repository is a marketplace whose one plugin lives in `plugins/`, so `--plugin-dir`
points at the plugin, not at the checkout root.

### How the plugin sets itself up

Two pieces, and a beginner never sees either:

**A `SessionStart` hook** runs `mcp/bootstrap.sh` at the start of every session. On the
first run it does two things, both into the plugin's own persistent data directory
(`~/.claude/plugins/data/<plugin>/`), touching nothing else on the machine:

1. Puts a private copy of [uv](https://docs.astral.sh/uv/) there — copying the one already
   on PATH if there is one, otherwise downloading it with the official installer.
2. Asks that uv to build the server's environment ahead of time: its own Python, plus the
   packages the server declares.

Every later run compares a hash of `server.py` to a stamp and does nothing unless the server
changed. On Windows the hook runs under Git Bash, which the Code tab already requires.

**A launcher** is what the server entry in `.mcp.json` actually starts: `mcp/launch` on macOS and Linux,
`mcp/launch.cmd` through `cmd.exe` on Windows — one entry, resolved per platform by
`${COMSPEC:-…}`. It exists before setup has run, which matters: Claude Code spawns the server
at the same moment the hook starts, and a spawn that fails is remembered for fifteen minutes.
The launcher waits for setup to finish (or runs it itself if the hook never fired), then hands
over to the private uv. Nothing depends on PATH, on a Python being installed, or on the
desktop app inheriting your shell environment.

The setup log is `bootstrap.log` in that data directory. If setup fails — usually because
the machine was offline — the agent is told so in words and relays it.

The server entry lives in `.mcp.json` beside the plugin manifest, and it is the only copy —
neither manifest repeats it. That file, not a `mcpServers` path in `plugin.json`, is what
every host reads: Claude Code picks up a plugin's `.mcp.json` on its own, and it is the only
form Grok reads at all. The entry needs the plugin variables a host sets only for plugins, so
working on this repository means running it as one — `claude --plugin-dir plugins/molecule-lab-contributor-agent`
from the checkout root. Run by hand, the launchers fall back to a gitignored `.plugin-data/`
folder inside the plugin directory.

The plugin sits in `plugins/` rather than at the repository root for the same
cross-host reason: a marketplace entry whose `source` is the root resolves in Claude Code but
not in Grok, which silently lists no plugin at all.

### How the Python dependencies get installed

There is no `requirements.txt`, and that is deliberate — **the dependency list lives inside
`mcp/server.py` itself**, in a [PEP 723](https://peps.python.org/pep-0723/) block at the top:

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0", "httpx>=0.27", "cryptography>=42", "eth-account>=0.13.7"]
# ///
```

uv reads that block, resolves the four dependencies (about 50 packages once transitive ones
are counted), fetches a Python that satisfies `requires-python`, and runs it — all into the
plugin's data directory, about 130 MB in total. Nothing is compiled from source, so no build
toolchain is needed on any platform.

One list, in one place, that cannot drift from the code that imports it.

**If the server does not connect**, read `bootstrap.log` in the plugin's data directory
first; then ask the agent to run `config_doctor`.

## Running it on another agent

Nothing here is specific to Claude. The whole flow lives in the `mol-labs` MCP server and
its two secrets; the skill file is just the playbook the driving agent follows. Any agent
that speaks MCP over stdio and can hold an eight-step plan can run it.

### Grok

[Grok Build](https://docs.x.ai/build/overview), xAI's CLI agent, reads Claude Code
marketplaces, plugins, skills and MCP servers directly, so this installs with the same two
lines and no config authoring:

```
grok plugin marketplace add moleculeprotocol/molecule-lab-contributor-agent
grok plugin install molecule-lab-contributor-agent --trust
```

If Grok answers that it "couldn't scan every marketplace" it is refusing to guess between
sources, not failing on this one — any unreachable marketplace you have added, from Claude or
otherwise, triggers it. Pin the source and it proceeds:

```
grok plugin install molecule-lab-contributor-agent@molecule-lab-contributor-agent-marketplace --trust
```

`--trust` is not optional. Without it Grok finds the plugin but leaves its hooks and its
MCP server inactive, which looks exactly like a broken install. Plugins are also off until
enabled — press <kbd>Space</kbd> on it in the `/plugins` modal, or list it in
`~/.grok/config.toml`:

```toml
[plugins]
enabled = ["molecule-lab-contributor-agent"]
```

Grok reads the skill too, so the agent gets the same playbook and the same confirmation
gate it has under Claude Code.

One thing to know about the first run. Grok gives an MCP server **30 seconds** to start,
and the first launch is the one that downloads a private Python and about fifty packages —
longer than that on a cold machine. The first session comes up with no `mol-labs` tools;
setup carries on in the background and the next session connects normally.

If you would rather not spend a session on that, register the server yourself with a longer
budget. It needs the whole entry, not just the timeout — an `[mcp_servers.*]` section
without a `command` is skipped. `grok plugin details molecule-lab-contributor-agent` prints
the install path:

```toml
[mcp_servers.mol-labs]
command = "<install path>/mcp/launch"
startup_timeout_sec = 600
```

**What will not work is Grok's hosted MCP** — the API's `mcp` tool type and the custom
connectors on grok.com. Both accept only Streamable HTTP and SSE, and they reject
`localhost` and private addresses outright. This server is stdio and holds your credential
and the agent's key on your machine, so there is nothing to point them at.

Please do not reach for a tunnel to get around that. Wrapping the server with
supergateway or ngrok does technically work, and it publishes an unauthenticated
upload-and-read tool surface — backed by the agent's signing key — on a public URL, to be
called by anyone who learns it. Use an agent that can start a local process instead.

### Any other MCP client

opencode, Goose, Cline, Codex and VS Code agent mode all spawn local stdio servers, and most
let you choose the model behind them. Clone the repository and register `mcp/launch` — not
`mcp/server.py`. The launcher installs its own private uv and Python on first run and passes
the plugin's paths to the server, so there is nothing to put on `PATH` and no environment to
set:

```json
{
  "mcpServers": {
    "mol-labs": {
      "command": "/absolute/path/to/molecule-lab-contributor-agent/mcp/launch"
    }
  }
}
```

Adapt the shape to whatever the client uses — a `command` and no arguments is all of it.
On Windows, run `mcp/launch.cmd` through `cmd.exe` instead.

Then give the agent `skills/molecule-lab-contributor/SKILL.md` as context. Claude Code and
Grok load it from the plugin; every other client needs it pasted in or referenced as a
system prompt, and without it the agent has the tools but not the rules — including the
one that matters, which is that it must never choose public or private for you.

Run this way, the plugin's data directory is a gitignored `.plugin-data/` inside the
checkout, and that is where `.env` and the agent's key live. Keep the checkout.

## Configuration

Two secrets, and the agent writes both for you:

```
"save my Molecule credential: mol_…"   -> writes MOLECULE_CONSUMER_CREDENTIAL to .env
"create your wallet"                   -> writes MOLECULE_AGENT_PRIVATE_KEY to .env
```

As a plugin, that `.env` lives in the plugin's persistent data directory
(`~/.claude/plugins/data/<plugin>/.env`), so it survives plugin updates and does not depend
on which folder you opened. `config_doctor` reports the exact path as `secretsFile`. Run
from a clone instead of as a plugin, it is `.env` in the project. It is created at mode
0600 and is gitignored. Copy `.env.example` if you would rather fill it in yourself. Two
gotchas worth knowing:

- On macOS `.env` is hidden in Finder — `Cmd+Shift+.` shows hidden files.
- The server reads configuration once at startup, so reconnect it (`/mcp`) after editing.

Everything else has a working default. `.env.example` lists the endpoint overrides, which
exist so the Molecule team can point an agent at another deployment for testing; you should
not need to touch them.

## Privacy and data

The server runs on your machine and talks to three places: the Molecule Labs API, the
file storage that API hands it a signed upload or download link for, and a public read-only
node for the configured chain, used to check a private file's access rule before encrypting.
There is no telemetry, no analytics, and nothing is sent anywhere else.

What leaves your machine is what you asked to upload, plus the Lab id and the agent's
address needed to do it. A private file leaves as ciphertext; its key goes to Molecule's
key service, which releases it only to wallets the Lab's access rule admits. Your `mol_…`
credential, the agent's key, and its token stay in the local `.env` and are redacted from
every tool result before the agent sees it.

How Molecule handles the data that reaches its services is covered by the
[Molecule privacy policy](https://molecule.xyz/privacy-policy).

## Support

- Bugs and questions: [GitHub issues](https://github.com/moleculeprotocol/molecule-lab-contributor-agent/issues)
- Molecule community: the [Molecule Discord](https://t.co/L0VEiy4Bjk)
- Security concerns: email the maintainer listed in `.claude-plugin/plugin.json` rather than opening a public issue.

## Scope

This is the **contributor** lane: the agent owns no Lab and creates none. To have an agent
mint and register a Lab of its own, use the
[`mol-labs-plugin`](https://github.com/moleculeprotocol/mol-labs-plugin) `aura-orchestrator`
skill instead.

## License

Apache-2.0 — see [LICENSE](LICENSE).
