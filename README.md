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
| **[uv](https://docs.astral.sh/uv/)** | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, or `brew install uv` |

uv is the only thing to install. It reads the dependency block at the top of the server and
provisions both the packages **and a Python to run them on** — no virtualenv, no `pip`, and
nothing compiled from source. The first run takes a few seconds; after that it is instant.

## Install

**Two things, in this order.** The plugin commands install the skill and the server; they do
not install uv, and without uv the server cannot start.

### 1. Install uv (once per machine)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS / Linux
# or: brew install uv
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version                                        # confirm it is on PATH
```

You do **not** need to install Python, create a virtualenv, or run `pip`. uv does all of it.

### 2. Install the plugin

```
/plugin marketplace add moleculeprotocol/molecule-lab-contributor-agent
/plugin install molecule-lab-contributor-agent@molecule-lab-contributor-agent-marketplace
```

or from a clone:

```bash
git clone https://github.com/moleculeprotocol/molecule-lab-contributor-agent.git
claude --plugin-dir /path/to/molecule-lab-contributor-agent
```

### How the Python dependencies get installed

There is no `requirements.txt`, and that is deliberate — **the dependency list lives inside
`mcp/server.py` itself**, in a [PEP 723](https://peps.python.org/pep-0723/) block at the top:

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0", "httpx>=0.27", "cryptography>=42", "eth-account>=0.13.7"]
# ///
```

`.mcp.json` starts the server with `uv run .../mcp/server.py`. uv reads that block, resolves
the four dependencies (about 50 packages once transitive ones are counted), fetches a Python
that satisfies `requires-python` if yours does not, and runs it — all into a shared cache.
First start takes roughly half a minute and about 130 MB; every start after that is instant.
Nothing is compiled from source, so no build toolchain is needed on any platform.

One list, in one place, that cannot drift from the code that imports it.

**If the server does not connect**, check `uv --version` first — a missing uv shows up only as
`command not found` in the MCP logs, which is easy to miss.

**Any other MCP-capable agent** — point it at `uv run /path/to/mcp/server.py` and give it
`skills/molecule-lab-contributor/SKILL.md` as context.

## Configuration

Two secrets, and the agent writes both for you:

```
"save my Molecule credential: mol_…"   -> writes MOLECULE_CONSUMER_CREDENTIAL to .env
"create your wallet"                   -> writes MOLECULE_AGENT_PRIVATE_KEY to .env
```

`.env` is created at mode 0600 and is gitignored. Copy `.env.example` if you would rather
fill it in yourself. Two gotchas worth knowing:

- On macOS `.env` is hidden in Finder — `Cmd+Shift+.` shows hidden files.
- The server reads configuration once at startup, so reconnect it (`/mcp`) after editing.

Everything else has a working default. `.env.example` lists the endpoint overrides, which
exist so the Molecule team can point an agent at another deployment for testing; you should
not need to touch them.

## Scope

This is the **contributor** lane: the agent owns no Lab and creates none. To have an agent
mint and register a Lab of its own, use the
[`mol-labs-plugin`](https://github.com/moleculeprotocol/mol-labs-plugin) `aura-orchestrator`
skill instead.

## License

Apache-2.0 — see [LICENSE](LICENSE).
