# Molecule Labs — agent skill

A [`SKILL.md`](skills/molecule-lab-contributor/SKILL.md) and a handful of small Python
scripts that let an AI coding agent work inside a **Molecule Lab that a human owns**.

The researcher creates their Lab in the [Labs app](https://labs.molecule.xyz) — email
sign-in, no wallet, no code. The agent then gets its **own** identity rather than
borrowing theirs:

| # | Actor | Action |
|---|-------|--------|
| 0 | Agent + **Human** | Agree what is being uploaded, where, and whether it is public or private |
| 1 | Agent | Generates a wallet, reports the address |
| 2 | **Human** | Adds that address to the Lab as **Contributor** |
| 3 | Agent | Polls until the grant is visible |
| 4 | Agent | Self-issues a service token by signing a sign-in message |
| 5 | Agent | Uploads — public, or client-side encrypted |
| 6 | Agent | Verifies, and hands back a link |

**The human never hands over a private key, a token, or their session.** Revoking the
agent is one on-chain revoke that touches nothing else.

## The agent asks before it publishes

Phase 0 is the point of this skill as much as the upload is. Publishing to a data room is
a one-way door — a public file is world-downloadable the moment it lands, a path can never
be reused, and nothing un-publishes what was already fetched.

So there are **no defaults for the choices that cannot be undone**. The agent has to have
an answer for the environment, the Lab, the visibility, the source file, the data-room path
and the description before it makes a single API call, and it states the whole plan back
before it starts. `agent_upload.py` enforces this: every missing answer is a refusal
carrying the question the agent should be asking you, not a silent default.

In particular, **the agent never picks public for you.**

## Public and private uploads

| | |
|---|---|
| **Public** | plaintext, `accessLevel: PUBLIC`. The read query is unauthenticated, so anyone can fetch the file. |
| **Private** | AES-256-GCM encrypted before it leaves the machine. The key is released only to wallets that pass the Lab's on-chain access conditions — by default the Lab owner and its Contributors. Note this is one notch tighter than the Labs web app, which also admits read-only Viewers; pass `--condition-role viewer` to match the app. |

The encryption is the same envelope the Labs web app uses, reproduced byte-for-byte and
pinned by a frozen known-answer vector, so a file the agent encrypts opens in the app and
vice versa. It is strong and rule-based, but
it is **not** zero-knowledge: Molecule operates the key service, so Molecule's
infrastructure can decrypt the file. The skill says so rather than overselling it.

Two things the skill is loud about, because both silently publish confidential data:

- `accessLevel` is a **label, not a lock**. Uploading plaintext and marking it `ADMIN`
  succeeds and stores your plaintext. Only encrypting encrypts.
- `accessLevel` is not validated on upload. A typo like `"private"` is accepted and reads
  back as `PUBLIC`.

## What it costs

Nothing, on either path. The agent's key signs exactly one off-chain message and never
sends a transaction — **no gas, no USDC, no x402**. Encryption adds no payment surface.
The only on-chain call in the whole flow is the human's role grant, and the Labs app
sponsors that gas.

## What you need

| | |
|---|---|
| **Consumer credential** | A `mol_<consumerId>_<secret>` string. Request one on the [Molecule Discord](https://t.co/L0VEiy4Bjk). |
| **`oclId`** | The Lab's 32-byte id, copied from the Lab in the app. |
| **[uv](https://docs.astral.sh/uv/)** | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, or `brew install uv` |

No MCP server and no build step. uv is the only thing to install: each script declares its
own dependencies inline, and uv provisions both those and a suitable Python — no venv, no
activation, no `pip install`, nothing to keep in sync.

The three dependencies are `eth-account`, `cryptography` and `httpx`. `eth-account` is not
small — it pulls compiled extensions — and that is a deliberate trade: the alternative is
hand-rolling secp256k1 signature normalisation and keccak256, and getting any of that
subtly wrong produces an `INVALID_SIGNATURE` you cannot debug. Curve code is not something
this skill should ask you to trust.

## Install

All install paths copy the whole skill directory, so the scripts and their inline
dependency blocks travel with it.

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

**OpenAI Codex** — copy `skills/molecule-lab-contributor/` into the skills directory your
Codex version scans (check `/skills`), or surface it through `AGENTS.md`.

**Any other agent** — `SKILL.md` is plain markdown and carries its own constants, GraphQL
documents and invariants; paste it into a system prompt or a context file, and keep
`scripts/` next to it so the agent can run the flow instead of re-typing it.

## Configuration

The skill reads its configuration from the process environment. Pass values inline, or
copy `.env.example` to `.env` and let uv load it:

```bash
cp .env.example .env      # then fill it in; .env is gitignored

# Identity first — no upload arguments needed, and none should be invented to get one.
uv run skills/molecule-lab-contributor/scripts/agent_upload.py --key-out ./.agent-key

# Then, once the Lab owner has granted that address the Contributor role:
uv run --env-file .env skills/molecule-lab-contributor/scripts/agent_upload.py \
  --file ./findings.csv --visibility private --path findings.csv \
  --description "Round 3 assay results" --category science --tag Discovery
```

Add `--dry-run` to rehearse the gate, the preflight and the encryption without any
data-room write (it does issue a service token, so nothing is published but it is not
inert). `uv run
skills/molecule-lab-contributor/scripts/selftest.py` verifies the encryption and the
access-condition shape offline, with no credentials and no network.

| | |
|---|---|
| `MOLECULE_ENV` | `staging` or `production` — **required, no default** |
| `CONSUMER_CREDENTIAL` | your `mol_<consumerId>_<secret>` — secret |
| `OCL_ID` | the Lab you were granted a role on |
| `AGENT_PRIVATE_KEY` | the agent's own key — secret. **Generate once and keep it**: a new key is a different agent with no role, and the owner would have to grant it again. |
| `SERVICE_NAME` | optional; how the human identifies your token when revoking |
| `EXPIRES_IN` | optional, default `30d`. Units `s m h d w` only — see the skill on why `M` is a trap. |
| `EVM_RPC_URL` | optional; a read-only Base RPC for the private path's preflight check |

## Environments

There is no default — a Lab created in one is invisible in the other, and guessing
produces a `NOT_FOUND` that looks like a bad `oclId`.

| | staging | production |
|---|---|---|
| app | `https://testnet.labs.molecule.xyz` | `https://labs.molecule.xyz` |
| API | `https://staging.graphql.api.molecule.xyz/graphql` | `https://production.graphql.api.molecule.xyz/graphql` |
| chain | Base Sepolia (84532) | Base (8453) |

Both support private uploads.

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
