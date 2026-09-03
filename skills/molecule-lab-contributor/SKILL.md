---
name: molecule-lab-contributor
description: Write files into a Molecule Lab that a human owns. Use when someone has made a Lab in the Molecule Labs app and wants their agent to put files in its data room — the agent creates its own wallet, they grant it the Contributor role, it issues its own token, and then it reads and writes the Lab. Uploads are either public (plaintext, anyone can download) or private (encrypted, only people with a role can open) — the human always chooses, and every upload is read back to them and confirmed before anything is written. This skill owns no Lab, creates none, and spends nothing.
license: Apache-2.0
---

# Molecule Lab Contributor

A human owns the Lab. You get your own identity, they grant it a role, and from then on you
authenticate as yourself. **You never receive their private key, their token, or their
session**, and they can revoke you with one click without touching anything else.

Everything runs through the `mol-labs` MCP server that ships with this skill. Each tool
returns a `next_step` telling you what to do and what to ask — follow it rather than
improvising, and stop where it tells you to stop.

If those tools are not available to you, **stop and say so.** Do not hand-roll an upload:
every safeguard described below lives in that server, not in this document.

---

## The one rule

**Never decide for the human whether a file is public or private.** Ask, every single time,
before anything is uploaded.

A data room is not a scratch directory. A public file is downloadable by anyone with the
link from the moment it lands, a path can never be reused, and nothing un-publishes what has
already been fetched. "They didn't say, so I published it" is the one outcome this skill
exists to prevent.

So there is no default visibility anywhere in this skill, and the server will not let you
invent one: `stage_upload` records what you are about to do and hands it back for the human
to approve, and every upload tool refuses to run until `confirm_upload_plan` has recorded
their actual answer.

Ask it in their words, not the API's:

> Should this be **public** — anyone with the link can download it, permanently — or
> **private** — encrypted, so only you and the Contributors on this Lab can open it?

If they are unsure, the honest framing is: *if it contains anything you would not put on a
public website, choose private.* Do not treat public as the safe default — for a research
data room it is the dangerous one.

> ### The access level is a label, not a lock
>
> These are two independent things and the server enforces no relationship between them.
> Marking a file private-looking while uploading plaintext **succeeds**, stores your
> plaintext, and shows a row that looks confidential. Nothing on the server encrypts
> anything. A file is encrypted only because it was encrypted before being uploaded.
>
> That is why the private path is its own tool (`upload_private_file`) rather than a flag.
> Use it, and never hand-roll an upload that merely sets the label.

---

## What the human needs

| | |
|---|---|
| **A Molecule API credential** | The `mol_…` string. **Think of it as an API key** — it identifies whose calls these are, counts against their quota, and can be revoked. It is not a wallet, holds no funds, and signs nothing. |
| **Their Lab** | Whichever Lab they want you writing into. Nobody needs to hunt for its id — see below. |
| **[uv](https://docs.astral.sh/uv/)** | The one thing they install, and installing the plugin does **not** install it: `curl -LsSf https://astral.sh/uv/install.sh \| sh`, or `brew install uv`. The server's dependencies are declared inside `mcp/server.py` itself, so uv fetches them — and a Python to run them on — the first time it starts. Nothing to configure, nothing compiled. If the server will not connect, check `uv --version` before anything else. |

### Where the API credential comes from

- Webinar and starter-pack users: **it is in the starter pack.** Ask them to look there first.
- Otherwise the Molecule team issues them — ask on the
  [Molecule Discord](https://t.co/L0VEiy4Bjk). There is no self-serve page, so do not send
  them hunting for one, and never try to generate one yourself.

Once they hand it over, store it with `save_credential(credential="mol_…")`, which writes it
to `.env` at mode 0600 so later sessions pick it up. Do not paste it back into the chat.

### Finding their Lab — just ask for the URL

The Lab's canonical id is a 32-byte hex string, and **the human should never have to go
looking for it.** Ask them to open their Lab in the app and paste the address bar:

```
https://labs.molecule.xyz/labs/their-lab-name
                               ^^^^^^^^^^^^^^ this is all that is needed
```

Give the whole URL to `resolve_lab` and it returns the id, the display name, and the Lab's
account address. The bare name works too, and so does the id itself if they happen to have
one.

Then **say which Lab you resolved** — "that's *Their Lab Name*, correct?" — so a wrong paste
is caught before an upload rather than after one.

### A note on `.env`

The two secrets live in a `.env` file beside the project, and the server reads it at
startup. Two things worth telling the human once:

- On macOS `.env` is **hidden in Finder** — `Cmd+Shift+.` toggles hidden files.
- The server reads its configuration **once, when it starts.** If they edit `.env`,
  reconnect the server (`/mcp` in Claude Code) or the change will look like it did nothing.

`save_credential`, `agent_wallet(create=True, …)` and `issue_service_token(…, envFile=…)` all
write into that file for them, so nobody has to hand-edit it.

---

## The flow

Eight steps. **Each one ends with something to tell or ask the human** — the point is not to
sprint to the upload, it is that nothing irreversible happens without them knowing.

| # | Tool | You do | You then |
|---|---|---|---|
| 1 | `config_doctor` | See what is configured | Show them `issues` and `fixes`, in that order |
| 2 | `save_credential` | Store their `mol_…` credential | Confirm it is saved; never echo it back |
| 3 | `agent_wallet(create=True)` | Create this agent's identity | Give them the address and **stop** |
| 4 | `resolve_lab` | Turn their Lab URL into an id | Confirm which Lab you found |
| 5 | `lab_members` | Check the grant landed | If it has not, wait — do not spin |
| 6 | `issue_service_token` | Authenticate this agent's writes | Tell them the token's name and expiry |
| 7 | `lab_info` → `file_categories_and_tags` → `stage_upload` | Prepare one upload | **Read the plan back and wait for a yes** |
| 8 | `confirm_upload_plan` → `upload_public_file` or `upload_private_file` → `verify_upload` | Do it, then prove it | Say the visibility out loud and give them the link |

**Credential before wallet.** Step 2 comes before step 3 for a reason: a wallet created
before the credential is in place cannot look anything up, and the next step then fails for
a reason that has nothing to do with the wallet. `config_doctor` orders its `fixes` that way
— work through them in order.

### Step 3 — hand over the address, then stop

`agent_wallet(create=True, envFile=".env")` returns an address and nothing else. Tell them:

> This agent's address is `0x…`. In the Labs app, open your Lab → **Members** → add that
> address with the **Contributor** role, then tell me when it's done.

Then **actually stop.** Only the Lab owner can grant a role, and a Viewer can read but never
upload, so Contributor is the one that matters. Do not poll in a silent loop.

The key is saved in `.env`. Tell them to keep it: a new key is a *different agent* with no
role, and they would have to grant it all over again.

### Step 7 — the confirmation gate

`stage_upload` takes every answer the human gave you and returns three things: a
plain-language `summary`, a list of `ask_the_human` questions, and any `warnings`. Put all of
it in front of them and **wait**.

If they approve, pass their own words to `confirm_upload_plan`. If they change anything — the
visibility, the path, the description — call `stage_upload` again with the correction.

Do not paraphrase an approval they never gave. The server rejects placeholders like `n/a`
precisely because that is the failure this whole design is guarding against.

### The other tools

Three you will not usually call yourself, listed so you know they exist:

- `check_onchain_access` — reads the Lab's access resolver directly to confirm a private
  file's lock will be evaluatable *and* will admit this agent. `upload_private_file` runs it
  automatically; call it on its own when diagnosing a decrypt failure.
- `envelope_self_test` — proves this server's encryption still matches the data-room format.
  No network, no credentials. Also run automatically before any private upload.
- `read_data_room_file` / `list_data_room_files` — reading the Lab, covered below.

### Step 8 — say what happened

`verify_upload` re-reads the committed file, and for a private upload it downloads the stored
bytes and decrypts them, because a successful upload proves nothing about the encryption on
its own.

Then tell them explicitly **which visibility it went up as**, and give them the link. If it
was private, ask them to confirm they can open it in the app — that is the only check that
proves the *owner*, not just this agent, can read it.

---

## Fail closed

Once a file has been chosen as private, **there is no public fallback.** If any part of the
private path fails, stop and report it.

Do not re-run it as public, do not upload the plaintext, do not finalize it with a public
label. The server refuses all three once a file has been encrypted and will not be talked
round — but the reason it has to refuse is that the API itself would happily accept them.

## If you published the wrong thing

Say so immediately, in your next message, before anything else. Then:

- A file's access level can be changed afterwards, and a path can be deleted — but **not
  through this skill**. It ships no tool for either; the human does both in the app.
- **Neither un-publishes anything.** Relabelling a file does not encrypt bytes that were
  stored in the clear, and it does not un-download what somebody already fetched.

Treat the content as disclosed and let the human decide what to do about it. This is exactly
why the gate exists.

---

## Reading the data room

A Contributor can read as well as write, which is what makes "analyse what's already here,
then write the results back" possible. `list_data_room_files` lists everything with its
access level and whether it is encrypted; `read_data_room_file` downloads one and decrypts it
if it needs to.

Note that the access level does not gate the *download*: a private file's ciphertext is
fetchable by anyone who can query the Lab. The confidentiality is in the encryption, not the
label — so never tell a human a private file "can't be downloaded". It can, as ciphertext
nobody can read.

## What "private" actually buys them

Be accurate about this if they ask, and do not oversell it. The file is AES-256-GCM
encrypted, its key is wrapped by a managed key service, and every release of that key is
gated by a fresh on-chain role check that fails closed. No other user, outsider or storage
host can read it, and the owner can revoke access at any time.

It is **not** zero-knowledge. Molecule operates the key service, so Molecule's own
infrastructure can decrypt the file. Never tell anyone "not even Molecule can open this."

By default a private file is openable by the Lab's **Contributors and its owner**, but not by
read-only **Viewers** — one notch tighter than a file the human uploads through the app
themselves. `stage_upload` warns you about this every time; if they want everyone they have
invited to be able to open it, pass `conditionRole="viewer"`.

---

## What this skill is not

You do not own a Lab and you never create one.

- Do not create a Lab. If none is named, ask which one — never make one to get unstuck.
- You need **no funds**: no gas, no tokens, no payment of any kind, on either the public or
  the private path. This agent's key signs exactly one off-chain message and never sends a
  transaction. If you find yourself needing a funded wallet, you are on the wrong path.
- Some things are the owner's alone — editing the Lab's own name and image, and signing its
  legal agreements. Do not attempt them; report them back instead.

---

## Troubleshooting

| Symptom | What it means | What to do |
|---|---|---|
| A tool says the API credential is not set | No credential yet | Ask for their `mol_…` from the starter pack, then `save_credential`. |
| "You are not authorized to make this call" | The credential is wrong, expired, or belongs to a different deployment than the Lab | Ask them to re-check it with the Molecule team. |
| A tool says the credential looks wrong | It was pasted with a `Bearer` prefix, or an extra API-key header was added | Send it verbatim and alone. |
| `No Lab named '…' here` | Wrong paste, or the credential is for a different deployment than the Lab | Ask for the full URL from the address bar of their Lab page. |
| The agent has no role even though they say they added it | The role indexer is still catching up | Wait and call `lab_members` again. Do not re-issue the token or ask for another grant. |
| The agent's address is not the one they granted | A second `agent_wallet(create=True)` would have replaced the identity — it now refuses unless you pass `replace=True` | Compare `agent_wallet()`'s address against the Members panel before blaming the indexer. |
| The agent holds `VIEWER` | A Viewer can read but never upload | Ask them to change it to Contributor, and do not retry until they have. |
| A write fails as unauthorized shortly after the grant | The same indexer lag, on the write path | Wait and retry. Re-issuing the token will not help. |
| `'…' already exists in this data room` | That path is taken, permanently | Ask for a different path, or add a new version with `ref=`. |
| The token expires far sooner than it claimed | `expiresIn` used the `M` unit, which the signer reads as *minutes* | Re-issue with `s`, `m`, `h`, `d` or `w` — e.g. `"30d"`. |
| `hasRole` reverted, or came back false | A private file's lock could not be evaluated, or would deny this agent | Stop. Do **not** fall back to a public upload — report it. |
| Decrypt fails with an access denial | **Not necessarily a permission problem.** The API evaluates a file's on-chain lock live and fails closed, so a slow or failing chain call is reported in exactly the same words as a real denial | Try again — the tools already retry a few times. Measured on a real Lab: a file whose lock evaluates true on chain was denied once and opened on the next attempt. Only a denial that survives several tries is worth investigating, and then check the wallet actually signed in before you touch any roles. |
| The Lab owner cannot open a private file | Most often the above, or they are signed in as a different wallet than the one that owns the Lab | Have them retry first. Then compare the wallet the app shows them against the owner address in the Members panel. |
| A change to `.env` seems to do nothing | The server read its configuration at startup | Reconnect it (`/mcp`). |
| An upload succeeded but a later check failed | The file **is** published | Say so plainly. Do not re-upload — the path is taken. |

## Revoking

The human revokes this agent with one click in the Members panel, or by letting the role
grant's expiry lapse. Either also ends its ability to open files it encrypted itself, so
mention that when they pick an expiry.

The token can be retired separately, and is scoped to the wallet that issued it.

## Three addresses, not interchangeable

- **This agent's wallet** — the address the human grants a role to, and the identity every
  permission check resolves to.
- **The human owner's wallet** — theirs alone. You never need it and never ask for it.
- **The Lab's own account** — the Lab's on-chain account, named in the lock on a private file.

The Lab's id is none of these. Its last 40 hex characters happen to *be* the Lab's account
address, which makes them look interchangeable as arguments. They are not, and swapping them
produces a private file nobody can open, silently. The server derives and cross-checks both
for you — never hand-assemble them.
