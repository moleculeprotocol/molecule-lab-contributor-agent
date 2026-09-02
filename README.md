# Molecule Labs — agent skill

A single [`SKILL.md`](skills/molecule-lab-contributor/SKILL.md) that lets an AI coding
agent work inside a **Molecule Lab that a human owns**.

The researcher creates their Lab in the [Labs app](https://labs.molecule.xyz) — email
sign-in, no wallet, no code. The agent then gets its **own** identity rather than
borrowing theirs:

| # | Actor | Action |
|---|-------|--------|
| 1 | Agent | Generates a wallet, reports the address |
| 2 | **Human** | Adds that address to the Lab as **Contributor** |
| 3 | Agent | Polls until the grant is visible |
| 4 | Agent | Self-issues a service token by signing a sign-in message |
| 5 | Agent | Uploads files to the data room |
| 6 | Agent | Verifies, and hands back a link |

**The human never hands over a private key, a token, or their session.** Revoking the
agent is one on-chain revoke that touches nothing else.

## What it costs

Nothing. The agent's key signs exactly one off-chain message and never sends a
transaction — **no gas, no USDC, no x402**. The only on-chain call in the whole flow is
the human's role grant, and the Labs app sponsors that gas.

## What you need

| | |
|---|---|
| **Consumer credential** | A `mol_<consumerId>_<secret>` string. Request one on the [Molecule Discord](https://t.co/L0VEiy4Bjk). |
| **`oclId`** | The Lab's 32-byte id, copied from the Lab in the app. |
| **Node 18+ and `viem`** | `npm i viem` |

No MCP server, no Python, no build step. The skill is one markdown file.

## Install

**Claude Code** — drop the skill in and it is picked up:

```bash
git clone https://github.com/moleculeprotocol/molecule-lab-contributor-agent.git
mkdir -p .claude/skills
cp -R molecule-lab-contributor-agent/skills/molecule-lab-contributor .claude/skills/
```

or load the whole repo as a plugin:

```bash
claude --plugin-dir /path/to/molecule-lab-contributor-agent
```

or install it from GitHub:

```
/plugin marketplace add moleculeprotocol/molecule-lab-contributor-agent
/plugin install molecule-lab-contributor-agent@molecule-lab-contributor-agent-marketplace
```

**OpenAI Codex** — copy `skills/molecule-lab-contributor/SKILL.md` into the skills
directory your Codex version scans (check `/skills`), or surface it through `AGENTS.md`.

**Any other agent** — the file is plain markdown. Paste it into a system prompt or a
context file; it carries its own constants, GraphQL documents and a complete runnable
script.

## Configuration

The skill reads three values from the process environment. Pass them inline, or copy
`.env.example` to `.env` and load it with Node's built-in flag — no dependency:

```bash
cp .env.example .env      # then fill it in; .env is gitignored
node --env-file=.env agent-upload.mjs ./findings.csv
```

| | |
|---|---|
| `CONSUMER_CREDENTIAL` | your `mol_<consumerId>_<secret>` — secret |
| `OCL_ID` | the Lab you were granted a role on |
| `AGENT_PRIVATE_KEY` | the agent's own key — secret. **Generate once and keep it**: a new key is a different agent with no role, and the owner would have to grant it again. |

`SERVICE_NAME`, `GRAPHQL_URL` and `LAB_APP_URL` are optional; the defaults are staging.

## Environments

Staging (Base Sepolia) is the default — nothing there costs real value:

```
GRAPHQL_URL   https://staging.graphql.api.molecule.xyz/graphql
LAB_APP_URL   https://testnet.labs.molecule.xyz
```

Production (Base) — swap these in, nothing else changes:

```
GRAPHQL_URL   https://production.graphql.api.molecule.xyz/graphql
LAB_APP_URL   https://labs.molecule.xyz
```

## Scope

This is the **contributor** lane: the agent owns no Lab and creates none. To have an
agent mint and register a Lab of its own — and pay per call through the x402 gateway —
use the [`mol-labs-plugin`](https://github.com/moleculeprotocol/mol-labs-plugin)
`aura-orchestrator` skill instead.

## Further reading

- [Roles & Permissions](https://docs.molecule.xyz) — Owner / Contributor / Viewer, expiry, the agent flag
- [Labs API](https://docs.molecule.xyz) — the full GraphQL surface
- [Authentication](https://docs.molecule.xyz) — consumer credentials and service tokens

## License

Apache-2.0 — see [LICENSE](LICENSE).
