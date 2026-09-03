---
name: molecule-lab-contributor
description: Work inside a Molecule Lab that someone else owns. Use when a human has created a Lab in the Molecule Labs app and wants their agent to upload files into it — the agent generates its own wallet, reports the address so the human can grant it the Contributor role, self-issues a service token, then reads and writes the Lab's data room. Uploads are either public (plaintext, world-readable) or private (AES-256-GCM encrypted, opened only by people with a role on the Lab) — the human chooses, always, and the agent never assumes. This lane owns no Lab, mints nothing, and spends nothing — no gas, no USDC, no x402. For creating or minting a new Lab, use a different skill.
license: Apache-2.0
---

# Molecule Lab Contributor

A human owns the Lab. You get your own identity, they grant it a role, and from then
on you authenticate as yourself. **You never receive the human's private key, token, or
session**, and they can revoke you with one click without touching anything else.

---

# Ask before you upload. Every time.

A data room is not a scratch directory. Publishing a file to one is a one-way door: a
public file is world-downloadable from the moment it lands, a path can never be reused,
and there is no delete that un-publishes what was already fetched.

So this skill has **no defaults for the choices that cannot be undone**. Not "public
unless told otherwise". Not "the filename it happens to have on disk". If you have not
been told, you do not know — go and ask.

**The six answers you must have before the first API call:**

| | Question | Why it has no default |
|---|---|---|
| 1 | **Which environment?** The app at `labs.molecule.xyz` (production) or `testnet.labs.molecule.xyz` (staging)? | A Lab that exists in one is invisible in the other. Guessing produces a `NOT_FOUND` that looks like a wrong `oclId`. |
| 2 | **Which Lab?** the `oclId` | You cannot infer it, and there is no "the obvious one". |
| 3 | **Public or private?** | The whole point. See below. |
| 4 | **What file?** An existing path on disk, or content you are about to generate? | If you are generating it, you must write it out, show the human what it says, and get an explicit go-ahead *before* uploading. Never generate-and-upload in one motion. |
| 5 | **Where in the data room?** the exact `path`, or `ref` for a new version | A taken path fails permanently. The local filename is a guess, not an answer. |
| 6 | **What does it say?** the `description` the human will read next to it | An untitled, undescribed file is indistinguishable from junk in someone else's data room. |

Then **state the whole plan back in one message and wait for a yes** — environment, Lab,
visibility, source file, data-room path, description, content type, category and tags.
Do not start the flow on silence, and do not start it on "sounds good" to a message that
did not name the visibility.

## Public or private — say what each one actually means

Ask it in the human's terms, not in the API's:

> Should this be **public** — anyone with the link can download it, permanently — or
> **private** — encrypted, so only you and the Contributors on this Lab can open it?

(If they want read-only Viewers to be able to open it too, say so and use
`--condition-role viewer` — see P3.)

- **Public** → `accessLevel: "PUBLIC"`, plaintext bytes, and `labWithDataRoomAndFiles` is
  an unauthenticated query, so the download URL is fetchable by anyone who asks for it.
- **Private** → you encrypt the bytes yourself before uploading, and the key is released
  only to wallets that pass the Lab's on-chain access conditions.

If the human is unsure, the honest framing is: *"if this contains anything you would not
put on a public website, choose private."* Do not pick for them, and do not treat a
public default as the safe one — for a research data room it is the dangerous one.

> ### `accessLevel` is a label, not a lock
>
> This is the single most expensive misunderstanding available here. `accessLevel` and
> encryption are **two independent dials**, and the server enforces no relationship
> between them.
>
> Uploading plaintext and finalising it with `accessLevel: "ADMIN"` **succeeds**, stores
> your plaintext, and gives you a row that looks confidential in the app. Nothing on the
> server encrypts anything. The only reason a file is encrypted is that *you* encrypted
> the bytes before the PUT and sent `encryptionMetadata`.
>
> Worse, `accessLevel` is typed `String!` and is **not validated** on
> `finishCreateOrUpdateFile`. Casing is normalised, so `"Admin"` is honoured — but any word
> outside those three, like `"private"` or `"confidential"`, is accepted by the mutation and
> then reads back as `PUBLIC`. The plausible-sounding value is the dangerous one.
>
> Send exactly `"PUBLIC"`, `"HOLDERS"` or `"ADMIN"`, and for anything confidential run the
> private path in full.

## What "private" buys, honestly

Say this plainly if the human asks, and never overstate it. The file is AES-256-GCM
encrypted at rest, the key is wrapped by AWS KMS, and every release of that key is gated
by a fresh on-chain role check that fails closed. No other user, no outsider and no
storage host can read it, and the human can revoke access on-chain at any time.

It is **not** zero-knowledge. Molecule operates the key service and holds the master key,
so Molecule's infrastructure can decrypt the file. Do not tell anyone "not even Molecule
can open this."

## Fail closed

Once a file has been chosen as private, **there is no public fallback.** If any step of
the private path fails and you cannot fix it in place, **stop and report the error.**

Do not "recover" by re-running the upload as public. Do not PUT the plaintext bytes. Do
not finalise with `accessLevel: "PUBLIC"` or without `encryptionMetadata`. The server will
happily accept every one of those — it is the only thing standing between a confidential
document and a public URL, and it is not standing there. You are.

## If you published the wrong thing

Say so immediately, in your next message, before anything else. Then:

- `updateFileMetadata` can change `accessLevel` on an existing file, and
  `deleteDataRoomFile` can remove a path. Both are available to a Contributor.
- **Neither un-publishes anything.** Flipping a file to `ADMIN` relabels the row; it does
  not encrypt bytes that were stored in the clear, and it does not un-download what was
  already downloaded.

Treat the content as disclosed and let the human decide what to do about it. This is why
the gate above exists.

---

## What this skill is not

This is the **contributor** lane. You do not own a Lab and you never create one.

- Do **not** mint a LabNFT. Do **not** call `createLab`. If no Lab is named, ask for the
  `oclId` — never create one to proceed.
- You need **no funds**: no gas, no USDC, no x402 payment. This is true of the private
  path too — encryption adds no payment surface. Your key signs exactly one off-chain
  message and never sends a transaction. If you find yourself needing a funded wallet,
  you are on the wrong path — stop and re-read this file.
- Owner-only surfaces you cannot reach as a Contributor: `updateLabNftMetadata`,
  `generateLabImageUploadUrl`, and the legal-agreement mutations. Do not try them.

## Prerequisites

| | |
|---|---|
| **Consumer credential** | A `mol_<consumerId>_<secret>` string. The human supplies it. Treat the whole string as a secret. |
| **`oclId`** | The Lab's canonical 32-byte id, e.g. `0x0101…0042`. The human copies it from the Lab in the app. |
| **[uv](https://docs.astral.sh/uv/)** | The only thing to install: `curl -LsSf https://astral.sh/uv/install.sh \| sh`, or `brew install uv`. It reads the dependency block at the top of each script and provisions the packages *and* a suitable Python — no venv, no activation, nothing to keep in sync. |

Read all three from the environment. Pass them inline, or put them in a `.env` (see
`.env.example` in this repo) and let uv load it:
`uv run --env-file .env scripts/agent_upload.py …`. Persist `AGENT_PRIVATE_KEY` there:
a new key on the next run is a different agent with no role on the Lab.

Secrets are read from the environment and are never accepted as flags — argv shows up in
`ps` and lands verbatim in an agent's transcript.

Never print the consumer credential, the service token, the data encryption key, or the
agent private key into your reply. Read them from the environment.

## Constants

There is no default environment. Ask which app the human made their Lab in (question 1
above), then take the whole column.

| | staging | production |
|---|---|---|
| `GRAPHQL_URL` | `https://staging.graphql.api.molecule.xyz/graphql` | `https://production.graphql.api.molecule.xyz/graphql` |
| `LAB_APP_URL` | `https://testnet.labs.molecule.xyz` | `https://labs.molecule.xyz` |
| chain | Base Sepolia (`84532`) | Base (`8453`) |
| `ACCESS_RESOLVER` | `0x5493F472602C87318EA5Eff753cDD593bf9bF559` | `0x89a14Be8f7824d4775053Edad0f2fA2d6767b72B` |
| condition `chain` string | `sepolia-base` | `base` |

The last two matter only on the private path. The `chain` string is checked against a
fixed list: `sepolia-base` and `baseSepolia` both work for Base Sepolia, `base` for Base.
`base-sepolia` does **not**, and neither does `"base sepolia"` with a space. A wrong value
here uploads fine and can never be decrypted.

Both environments support private uploads.

## Headers

```
Content-Type:    application/json
Authorization:   mol_<consumerId>_<secret>     # NEVER prefixed with "Bearer"
x-service-token: <JWT>                         # mutations; omit entirely until you have one
```

Two layers, and it helps to keep them apart. `Authorization` decides *whether you may talk
to the API at all*; `x-service-token` decides *who you are inside a resolver*. Four rules,
each of which has broken a real run:

1. **No `Bearer`** in front of a `mol_` credential. The gateway accepts the string, then
   routes anything beginning with `Bearer ` to the Privy JWT verifier, which tries to
   base64-decode your credential as a token and denies it. It is not a malformed
   credential — it is the wrong branch.
2. **Never send `x-api-key` as well.** The API's default auth mode is the shared API key,
   so a request carrying both headers is resolved under the shared key and your consumer
   credential is never even seen. The request may still succeed — which is the problem:
   you silently lose per-consumer attribution, expiry and revocation.
3. **Omit `x-service-token` rather than sending an empty one.** Public queries need only
   `Authorization`.
4. **Send `x-service-token` on every mutation.** Without it, the resolver falls through to
   the Privy user path and hands your `mol_` credential to Privy as a session token, which
   fails as `UNAUTHENTICATED`. If a mutation you believe is correctly authorised returns
   `UNAUTHENTICATED`, check for a missing service token before you touch the credential.

The header name is read as exactly `x-service-token` or `X-Service-Token`; any other
casing is ignored. There is no `x-wallet-address` in this lane — on the service-token path
it is read but never used for authorisation.

## Error contract

- **Queries throw.** Failure lands in top-level `errors[]`; branch on `errors[i].errorType`.
  A bad or expired consumer credential shows up here as `UnauthorizedException`.
- **Mutations return errors in-band.** Every result type has `error: ApiError`.
  **Success ⇔ `error == null`** — never a truthy payload field, never a `message` string.
  There is no `isSuccess` field on any of these types any more; selecting one is a
  validation error that fails the whole request. Select
  `error { code message requestId retryable details }` on every mutation.
- **Parse `details` tolerantly.** It is an object on thrown query errors, a JSON string
  in-band, and currently a *doubly-encoded* JSON string in-band — one `JSON.parse` there
  returns a string and `.reason` is silently `undefined`. Loop until it is not a string.
- Branch on `code`, never on `message`. Retry only when `retryable` is true. Quote
  `requestId` in any bug report.
- Codes: `UNAUTHENTICATED`, `UNAUTHORIZED`, `NOT_FOUND`, `VALIDATION_FAILED`, `CONFLICT`,
  `FAILED_PRECONDITION`, `COMPLEXITY_LIMIT_EXCEEDED`, `RATE_LIMITED`\*, `TIMEOUT`\*,
  `UPSTREAM_UNAVAILABLE`\*, `INTERNAL_ERROR`\* (\* = retryable). Treat an unrecognised
  code as non-retryable, and surface it rather than swallowing it.
- **`retryable` is a hint, not a promise.** Two failures on this path carry retryable codes
  and are permanent: a taken path (`UPSTREAM_UNAVAILABLE` / `MoleculeDataRoomPathOccupied`)
  and malformed encryption metadata (`INTERNAL_ERROR` with `details.reason:
  "UPLOAD_FINISH_ERROR"`). Both are in the troubleshooting table.

---

# The flow

| # | Actor | Action |
|---|---|---|
| 0 | **Agent + Human** | Collect and confirm every input above |
| 1 | Agent | Generate a wallet, report the address |
| 2 | **Human** | Add that address to the Lab as **Contributor** |
| 3 | Agent | Poll until the grant is visible |
| 4 | Agent | Self-issue a service token |
| 5 | Agent | Upload — public (5A) or private (5P), never both |
| 6 | Agent | Verify, and give the human a link |

**Run the phases in this order.** Phase 3 must complete before phase 4 — see the note on
the nonce there. It is the single most common way this flow fails.

## Phase 1 — Generate the agent wallet

```python
from eth_account import Account

# Ask the human WHERE to store a key before generating one. A key with nowhere to live
# wastes the owner's role grant: the next run would be a different agent.
account = Account.create()
key_hex = account.key.hex()
if not key_hex.startswith("0x"):      # eth-account >=0.13 returns bare hex
    key_hex = "0x" + key_hex
fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never truncate
with os.fdopen(fd, "w") as handle:
    handle.write(f"AGENT_PRIVATE_KEY={key_hex}\n")
print("Agent wallet:", account.address)
# Then STOP. The address needs a role before anything else can happen.
```

**Write the key, do not print it.** Ask where it should go — an `.env` the human controls,
or their secret store — write it there with mode `0600`, and report only the path and the
address. A key echoed into a chat transcript is a key you have to rotate.

The signing wallet must be a plain EOA. Sign-in verification recovers the signer from an
EIP-191 signature and has no smart-contract-account fallback, so a Safe or a
contract-wallet address can never authenticate here. A generated key is fine.

## Phase 2 — Report the address, then stop

Tell the human, in plain terms, and explain the two settings rather than making them guess:

> My wallet address is `0x…`. In the Labs app, open your Lab → Members → add this address
> with the **Contributor** role.
>
> **Expiry** is a dropdown — Permanent, a preset number of days, or a custom date, and
> Permanent is the default. It is a hard cut-off: once it passes, my uploads start failing
> *and* I lose access to files I encrypted. Pick what matches how long you want me working
> on this, and tell me which you chose so I can match my token to it.

Then **stop and wait**. Do not poll silently for minutes with no output, and do not try
to grant the role yourself — only the Lab **Owner** can grant Contributor.

Why Contributor and not Viewer: a Viewer can read the data room and decrypt files locked
at Viewer level (what the app writes), but cannot upload. Uploading requires Contributor.

The human's grant is one on-chain call on the `AccessResolver`, and the app sponsors the
gas:

```solidity
function grantRole(bytes32 oclId, address account, uint8 role, uint64 expiry, bool isAgent);
// role: 2 = ROLE_CONTRIBUTOR, 1 = ROLE_VIEWER
```

## Phase 3 — Poll until the grant is visible

Public query — `Authorization` only, no service token needed.

```graphql
query ListLabMembers($oclId: String!) {
  listLabMembers(oclId: $oclId) {
    message
    members { walletAddress role source isAgent expiry grantedAt }
  }
}
```

This is a **query**, so it has no `error` field — a failure arrives as a thrown
top-level `errors[]`, not in the payload. (Some older published examples show
`isSuccess` / `error` here; that contract was removed.)

Match on `walletAddress.toLowerCase() === agentAddress.toLowerCase()` — addresses come
back lowercased. Poll every 5s for up to ~5 minutes.

- `role: "CONTRIBUTOR"` (or `"OWNER"`) → proceed to phase 4.
- `role: "VIEWER"` → **stop and say so.** Do not retry; a Viewer will never be able to
  upload. Ask the human to change the role to Contributor.
- No entry at all → the grant has not landed (or was never made, or has expired —
  expired grants are excluded from this list entirely). Keep polling, then ask. If it
  never appears, confirm you are pointed at the environment the human used.

`isAgent` merely echoes the flag the owner set; `false` there changes nothing about what
you may do, so do not treat it as a failed grant. `expiry` is unix seconds as a decimal
**string**, or `null` for a permanent grant — parse it before comparing.

## Phase 4 — Self-issue a service token

**Do this only after phase 3 has succeeded.** The sign-in message embeds a **single-use
nonce that expires 10 minutes after it is issued**. If you fetch it and then wait for the
human, it will be dead by the time you sign it.

```graphql
query GetServiceSignInMessage($walletAddress: String!, $serviceName: String!) {
  getServiceSignInMessage(walletAddress: $walletAddress, serviceName: $serviceName) {
    message
    expiresAt
  }
}
```

Sign the returned `message` **verbatim**, as a plain personal message (EIP-191
`personal_sign` — **not** typed data). Do not reformat it, do not trim it, do not rebuild
the string yourself: the server recomposes it byte-identically and any difference fails
verification.

```python
from eth_account import Account
from eth_account.messages import encode_defunct

signature = Account.sign_message(encode_defunct(text=message), private_key).signature.hex()
if not signature.startswith("0x"):   # eth-account >=0.13 returns bare hex
    signature = "0x" + signature
```

The signing wallet must be a plain EOA — verification recovers the signer from the
signature and has no smart-contract-account fallback, so a Safe can never authenticate here.

```graphql
mutation GenerateServiceToken(
  $serviceName: String!
  $walletAddress: String!
  $messageSignature: String!
  $expiresIn: String
) {
  generateServiceToken(
    serviceName: $serviceName
    walletAddress: $walletAddress
    messageSignature: $messageSignature
    expiresIn: $expiresIn
  ) {
    token tokenId expiresAt
    error { code message requestId retryable details }
  }
}
```

- `serviceName` is free-form, and it is how the human tells your token apart from every
  other agent's when they come to revoke one. Derive something identifying from the task
  rather than reusing a generic name, and say what you chose.
- The nonce is one per `(wallet, serviceName)` and issuing a new one overwrites the last,
  so two concurrent sign-ins under the same `serviceName` clobber each other.
- `expiresIn` is `<int><unit>`, bounds 1 hour to 2 years. **Use only `s`, `m`, `h`, `d`,
  `w`.** The `M` unit is a trap: the API stores and reports it as months while the token
  signer reads it as *minutes*, so `"6M"` returns an `expiresAt` six months out and dies
  in six minutes. `"y"` also drifts. Prefer `"30d"`, or match the role grant's expiry.
- The token goes in `x-service-token` on everything from here on. **It is wallet-bound,
  not Lab-bound** — authorisation is resolved per request from your wallet's role on the
  Lab you name, so one token works across every Lab you hold a role on.

Issuance is **not** gated on holding a role — any wallet can mint a token for itself. The
role is what makes the token *useful*. A token issued before the grant lands keeps
working once it does; you never need to re-issue it because of a permissions error.

## Phase 5 — Upload

Both paths share the same three calls; the private path adds encryption around them.
**Pick one per file and do not cross over.**

### Preflight (both paths)

Run the public read query once before uploading anything. It costs one round trip and
turns two permanent failures into questions:

```graphql
query Preflight($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    shortname labAccountAddress
    dataRoom { files { path } }
  }
}
```

- A `null` result means the Lab is not registered here — wrong `oclId`, or wrong
  environment. This query is nullable and does not throw for a missing Lab.
- If your intended `path` is already in `dataRoom.files`, **stop and ask**. Re-using a path
  fails permanently (see the troubleshooting table); the fix is a different path or a new
  version via `ref`, and both are the human's call.
- `labAccountAddress` is what the private path needs in its access conditions. Keep it.

### 5A — Public upload

**A — initiate:**

```graphql
mutation Initiate($oclId: String!, $contentType: String!, $contentLength: Int!) {
  initiateCreateOrUpdateFile(oclId: $oclId, contentType: $contentType, contentLength: $contentLength) {
    uploadToken uploadUrl uploadUrlExpiry method headers { key value }
    error { code message requestId retryable details }
  }
}
```

**B — PUT the raw bytes** to `uploadUrl` using the returned `method` and **exactly** the
returned `headers`, converted from the `[{key,value}]` array to an object. Do not add,
drop, or reorder headers. The URL is short-lived — read `uploadUrlExpiry` from the initiate
result rather than assuming a duration, and re-run initiate rather than PUTting past it.
This call goes to S3, not to the API — send no `Authorization` and no `x-service-token`.

**C — finish:**

```graphql
mutation Finish(
  $oclId: String!, $uploadToken: String!, $path: String, $ref: String,
  $accessLevel: String!, $changeBy: String!, $description: String,
  $tags: [String!], $categories: [String!], $contentText: String,
  $encryptionMetadata: EncryptionMetadataInput
) {
  finishCreateOrUpdateFile(
    oclId: $oclId, uploadToken: $uploadToken, path: $path, ref: $ref,
    accessLevel: $accessLevel, changeBy: $changeBy, description: $description,
    tags: $tags, categories: $categories, contentText: $contentText,
    encryptionMetadata: $encryptionMetadata
  ) {
    datasetId contentHash version
    error { code message requestId retryable details }
  }
}
```

Declare `$path` and `$ref` as nullable and send exactly one of them; a `String!` here makes
the documented `ref` alternative unreachable.

- `contentType` is **stored and shown to the human** — send the file's real MIME type
  (`text/csv`, `application/pdf`, `image/png`). Do not fall back to
  `application/octet-stream` silently: if you cannot infer it, ask, or the file lands in
  the data room as an unidentified blob.
- `accessLevel`: `"PUBLIC"` | `"HOLDERS"` | `"ADMIN"`, exactly. Re-read the box above
  about typos before you send this.
- `changeBy`: `did:ethr:<your wallet address>` — this is what the Labs app sends, and its
  file viewer only renders "By <address>" when it can parse that form. A bare address
  uploads fine and then shows no attribution at all.
- `path` for a **new** file; `ref` for a **new version** of an existing file. One or the
  other, never both. `ref` is the `datasetId` a previous finish returned, which is the
  same value that comes back as the file's `did` on read.
- **Re-using an existing `path` fails.** A second upload to the same path returns
  `UPSTREAM_UNAVAILABLE` / `MoleculeDataRoomPathOccupied` — "Path is occupied". Despite
  that code sitting on the retryable list, **this is permanent**: retrying can never
  succeed. Use `ref` for a new version, or choose a different path.
- The stored path comes back **with a leading `/`** — you send `findings.csv` and
  `dataRoom.files` reports `/findings.csv`. Normalise both sides to one leading slash and
  compare **exactly**; a suffix match would let `/2024-findings.csv` satisfy a check for
  `findings.csv` and silently verify the wrong file. Whichever form you send, use the
  stored string later for `decryptDataKey(filePath:)`.
- The API forwards `path` to the data room untouched and validates no characters itself, so
  underscores go through — but the schema docstring discourages them and the data room has
  its own path scalar, so prefer hyphens.
- `agreements/` is reserved by the backend; writing there fails as `UNAUTHORIZED` with
  `details.reason: "PROTECTED_PATH"`.
- `categories` / `tags`: one category, **at most 3 tags**, each tag must belong to that
  category, and both are matched **exactly** — category names are lowercase, tag names are
  Title-Case. Take the valid values from the public `fileCategoriesAndTags` query rather
  than inventing them or copying a list from elsewhere; it is served from a CMS and drifts:

  ```graphql
  query FileCategoriesAndTags { fileCategoriesAndTags { data { name tags } } }
  ```

  Validation runs before the data-room commit, so a rejected finish can simply be retried
  with corrected values — the S3 upload is still good. It also fails *open* when the CMS is
  unreachable, so a successful upload is not proof your tags were valid.
- `description` is what the human reads next to the file. Send it.
- `contentText` is optional searchable text used for semantic search. **On a private file
  it is stored in the clear** next to the ciphertext — see 5P.

### 5P — Private upload (encrypted)

Use this **instead of** 5A. It is the flow the Labs web app runs when a human ticks
"confidential". The **encryption** is reproduced byte for byte, so a file you write here
opens in the app and a file the app writes opens here. The **access conditions** are
deliberately one notch tighter than the app's — see P3.

No MCP server, no payment, no extra dependency beyond the three the script already declares.

**Crypto invariants — deviating from any of these produces a file nobody can open, with
no error at upload time:**

| | |
|---|---|
| algorithm | AES-256-GCM, no AAD (never call `setAAD`) |
| IV | random **12** bytes, fresh per file (not 16) |
| auth tag | 128-bit, **appended** to the ciphertext — there is no separate tag field |
| DEK | standard padded base64 of raw 32 bytes (not base64url) |
| `iv` | standard padded base64 of the raw 12 bytes |
| `contentHash` | lowercase hex SHA-256 of the **plaintext**, no prefix |
| `contentLength` | the **ciphertext** length = plaintext + 16 |

The `contentHash` rule is worth repeating because the schema fights you on it: the field's
own description says "Hash of the encrypted content". It is wrong. The client hashes the
plaintext, and nothing on the server checks the value — so getting it wrong is silent.

**P0 — check the lock before building it.** Read `hasRole(oclId, yourAddress, 2)` on the
`ACCESS_RESOLVER` over a public RPC (`https://sepolia.base.org` / `https://mainnet.base.org`).
This is the exact call the backend will make at decrypt time, so it answers two questions
at once:

- A **revert** — wrong `oclId`, wrong resolver, wrong chain — means the conditions you are
  about to write would be unevaluatable. The fail-closed evaluator would deny the whole
  array and the file would be permanently unopenable.
- A clean **`false`** — most often an expired grant — means the conditions would deny *you*,
  so you could not read your own file back.

Either way: abort, and do not fall back to public.

**P1 — generate a data encryption key:**

```graphql
mutation GenerateDataEncryptionKey {
  generateDataEncryptionKey {
    plaintextDEK encryptedDek encryptionSystem
    error { code message requestId retryable details }
  }
}
```

This needs only a valid service token — no role on any Lab, and it takes no `oclId`. So
success here proves nothing about your access; the gate is `decryptDataKey`, later.

`plaintextDEK` is a secret with a lifetime of a few lines. Hold it in a local variable,
never log it, never put it in a file, never mention it in your reply.

**P2 — encrypt locally.** `scripts/kms_envelope.py` is the implementation, verified
byte-compatible with the Labs client in both directions against a frozen known-answer
vector. Its `self_test()` runs automatically before the first confidential upload — a
confidential file is not the place to discover that the crypto drifted.

**P3 — build the access conditions.** These are the lock — the shape is in
`scripts/access_conditions.py`:

```
[ hasRole(oclId, :userAddress, 2)  ,  {"operator":"or"}  ,  isAuthorizedSignerForTba(:userAddress, labAccountAddress) ]
```

- `:userAddress` is a literal placeholder the backend substitutes at decrypt time. Leave it.
- **Role `"2"` (Contributor) is the default here, and it is a deliberate departure from the
  Labs app, which writes `"1"` (Viewer).** `hasRole` means "at least this role", so `2`
  admits Contributors and the Lab owner but **not Viewers**. A read-only collaborator can
  open files the owner uploads through the app and *cannot* open files uploaded through
  here. That is invisible until a Viewer tries to read, so **say which lock you used when
  you hand back**, and pass `--condition-role viewer` when the human wants the file readable
  by everyone they have invited.
- Keep the second clause. It names the Lab's own account explicitly and is the owner's way
  in; whether the first clause alone would admit the owner depends on the chain, so relying
  on it would make a file's readability by its own owner vary by environment.
- `labAccountAddress` comes from the preflight query. It is also the low 20 bytes of the
  `oclId` (`"0x" + oclId[-40:]`), computed by the API the same way — so derive it as a
  cross-check and stop if the two disagree. Note they are **not** interchangeable as
  arguments: swap them and the ABI encoding throws inside the evaluator, which fails closed
  and denies the entire array, owner clause included.
- Nothing on the server checks that these conditions refer to *this* Lab. Re-read them
  before sending and assert the `oclId` and `labAccountAddress` are the ones you meant.

**P4 — initiate** exactly as in 5A, but with `contentLength` = the **ciphertext** length.
Sending the plaintext length produces a presigned URL the body does not match and a bare
403 that looks like a header problem.

**P5 — PUT the ciphertext.** The bytes that leave the process are `ciphertext‖tag`. The
data-room `path` keeps the original filename — no `.enc` suffix.

**P6 — finish** as in 5A, with `accessLevel: "ADMIN"` (what the app uses for confidential
files) and `encryptionMetadata`:

| Field | Value |
|---|---|
| `encryptionSystem` | echo the value from P1; if it is null send the literal `"kms"` |
| `accessControlConditions` | `JSON.stringify(conditions)` — a stringified **array** |
| `encryptedBy` | your wallet address |
| `encryptedAt` | `new Date().toISOString()` |
| `encryptedDek` | from P1, already base64 — do not re-encode |
| `iv` | from P2, base64 |
| `contentHash` | from P2, hex SHA-256 of the plaintext |

All seven are required together. **Never omit `encryptionSystem`** — absent does not mean
"none", it routes the payload to a legacy validator that demands a completely different
field set and rejects it.

**P7 — verify by actually opening it.** A successful finish proves nothing about your
crypto: the backend never validates `encryptionMetadata` against the bytes, and its own CI
fixture happily uploads random junk in those fields. Download the stored file, call
`decryptDataKey`, decrypt, and assert the plaintext SHA-256 equals the `contentHash` you
sent. Then ask the human to confirm they can open it in the app — that is the only check
that proves the *owner*, not just you, can read it.

If anything fails from here on, remember the file **is already committed**. Say so. Telling
the human nothing was published when a confidential file is sitting in their data room is
its own kind of harm.

```graphql
mutation DecryptDataKey($oclId: String!, $filePath: String!) {
  decryptDataKey(oclId: $oclId, filePath: $filePath) {
    plaintextDEK iv message
    error { code message requestId retryable details }
  }
}
```

Use `dek.iv ?? file.encryptionMetadata.iv`, and fail if both are absent. Never derive or
regenerate an iv on the decrypt path.

### Who can open a private file

Decryption is **two gates**, and the caller for both is the **service token's
`adminAddress`** — the wallet that signed the sign-in message. Not `changeBy`, not any
header.

1. **Membership**, read from an indexed database: your wallet must hold at least Viewer on
   the Lab. Contributor satisfies it.
2. **The on-chain conditions** stored with the file, evaluated live against that same
   address.

Your Contributor grant passes both, so you can read back what you wrote. To decrypt *as*
someone else you would need a service token issued by their wallet — there is no way to
ask on their behalf.

The two failures look alike and mean opposite things. Both surface as
`error.code: "UNAUTHORIZED"`, so **branch on `error.details.reason`**:

- `reason: "UNAUTHORIZED"` → gate 1. Almost always the indexer catching up after a fresh
  grant. Wait and retry; the chain is already correct.
- `reason: "ACCESS_DENIED"` (message "Access denied by on-chain conditions") → gate 2. The
  conditions genuinely do not admit this wallet, or they are malformed — a wrong `chain`
  string or resolver address produces this identical error. Retrying never helps.

### Retry `UNAUTHORIZED` right after the grant

Role state reaches the API through an event indexer, so for a window after the human's
grant confirms on-chain a write still returns `UNAUTHORIZED`. **This is not a permissions
problem.** Re-issuing the token will not help, and asking the human to re-grant will not
help. Wait and retry. Usually seconds; it has taken minutes. The same lag delays your own
decrypt, so a private upload's verify step may need the same patience.

```python
# Retry only the failures that genuinely clear on their own. Inspect the structured code
# and details.reason rather than pattern-matching a message: several permanent failures
# share a code with the lag, and retrying those forever helps nobody.
LAGGY = {"UNAUTHORIZED", "NOT_FOUND"}
PERMANENT = {"PROTECTED_PATH", "ACCESS_DENIED", "OCL_NOT_FOUND"}

for attempt in range(12):
    try:
        return do_upload()
    except ApiError as err:
        if err.code not in LAGGY or err.reason in PERMANENT or attempt == 11:
            raise
        time.sleep(min(2 * 2**attempt, 30))   # 2s, 4s, 8s, 16s, then 30s
```

## Phase 6 — Verify and hand back

```graphql
query Verify($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    oclId shortname name
    dataRoom { files { did path contentType accessLevel version createdBy } }
  }
}
```

Public query, `Authorization` only. Confirm your `path` is in `dataRoom.files`, that
`accessLevel` is what you intended, and that `createdBy` matches your address. For a new
version, match on `did === datasetId` rather than on the path.

Then give the human the link: `<LAB_APP_URL>/projects/<shortname>`. They will see the
file in the data room, attributed to your address.

State the visibility explicitly when you hand back — "uploaded as **private/encrypted**"
or "uploaded as **public**". The whole point of the gate is undone if the human has to
open the app to find out which one happened.

## Reading the data room

A Contributor can read as well as write — useful when the job is "analyse what's already
in the Lab, then write the results back". Public query, `Authorization` only:

```graphql
query ReadDataRoom($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    shortname
    dataRoom {
      files {
        did path contentType contentHash version createdBy accessLevel
        downloadUrl downloadHeaders { key value } downloadUrlExpiry
        encryptionMetadata { encryptionSystem encryptedDek iv contentHash accessControlConditions }
      }
    }
  }
}
```

`GET` the `downloadUrl` with exactly the returned `downloadHeaders` to fetch the bytes.
The URL is presigned and short-lived — read `downloadUrlExpiry` and re-run the query
rather than caching the URL.

```python
headers = {h["key"]: h["value"] for h in (file.get("downloadHeaders") or [])}
body = httpx.get(file["downloadUrl"], headers=headers).content
```

**Two different fields are called `contentHash`.** `file.contentHash` is the data room's
own opaque digest of the bytes it stored — for an encrypted file that is the *ciphertext*.
`file.encryptionMetadata.contentHash` is the hex SHA-256 of the *plaintext*. They can never
be equal for an encrypted file. Compare the first between reads, never recompute it; use
the second only to check your own decrypt round-trip.

A file with `encryptionMetadata` is encrypted: download the ciphertext, call
`decryptDataKey`, decrypt with `scripts/kms_envelope.py`. A file whose stored
`encryptionSystem` is absent or `"lit"` returns `LEGACY_ENCRYPTION` — that is terminal, it
predates this envelope format and cannot be opened through this flow. Report it and move on.

Note that `accessLevel` does not gate the download: the ciphertext and its metadata are
readable by anyone who can run this query. That is fine — the confidentiality is in the
encryption, not in the row — but it means you should never tell a human that a non-public
file "cannot be downloaded". It can; it just cannot be read.

---

## Scripts

This skill ships the flow as runnable files rather than as something to transcribe:

| | |
|---|---|
| `scripts/agent_upload.py` | the whole flow, both paths. Refuses to run without the answers from the gate. |
| `scripts/labs_api.py` | every network call: GraphQL, S3 transfers, the chain read, retries, redaction |
| `scripts/kms_envelope.py` | AES-256-GCM envelope, byte-compatible with the Labs client |
| `scripts/access_conditions.py` | the access-condition array, plus a check that it names this Lab |
| `scripts/selftest.py` | offline checks for the two modules above — no network, no credentials |

uv is the only prerequisite. It reads the dependency block at the top of each script and
provisions the packages **and a Python to run them** — the human does not need Python
installed, does not need a virtualenv, and never runs `pip`.

**Check for it before you start**, so a missing tool is one message rather than a
mid-flow surprise:

```bash
uv --version || curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Homebrew: brew install uv
# Windows:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

A bare `uv: command not found` means exactly that and nothing else. Running the scripts
with `python3` directly instead will fail on a missing dependency — the script says so and
points back here rather than printing a traceback.

Nothing is compiled: the scripts pin `no-build`, so uv always resolves to a version with a
prebuilt wheel for the machine it is on. First run downloads ~110 MB and takes a few
seconds; every run after that is instant.

```bash
# 1. Identity. No upload arguments — you do not need a visibility to get an address,
#    and you must not invent one to get past the gate.
uv run scripts/agent_upload.py --key-out ./.agent-key

# 2. Hand the printed address to the Lab owner, and wait.

# 3. Upload, once every answer in the gate is the human's:
uv run --env-file .env scripts/agent_upload.py \
  --file ./findings.csv \
  --visibility <public|private — the human's answer, never yours> \
  --path findings.csv \
  --description "<the human's one-line description>" \
  --category science --tag Discovery

# Rehearse: add --dry-run. It runs the gate, the catalogue check, the preflight and the
# encryption, and stops before any data-room write. It does issue a service token (and,
# on the private path, a data key) — so nothing is published, but it is not inert.
# Verify the crypto and the conditions offline:
uv run scripts/selftest.py
```

`--help` prints the full flag and environment surface. Every missing answer is a refusal
carrying the question you should be asking the human, not a default.

Exit codes: `0` uploaded · `1` error · `2` waiting on the owner's role grant · `3` refused
by the input gate.

If you adapt the script, keep four structural properties — they are the guard, and prose
alone is what failed here before:

1. `upload_bytes` is assigned once. On the private path it holds the ciphertext, and it is
   the only thing the PUT can send.
2. The finish call refuses `PUBLIC` or a missing `encryptionMetadata` when the run is
   confidential.
3. The gate lives in `upload_file`'s signature, not only in the argument parser — so
   calling it from a notebook cannot bypass it.
4. A failure before the finish call says the file was **not** published; a failure *after*
   it says the file **is** published. Reporting a committed file as unpublished is its own
   kind of harm.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `UnauthorizedException` on the first query | The consumer credential is wrong, expired, or has a `Bearer` prefix | Send it bare. Ask the human to check it. |
| `UNAUTHENTICATED` on a mutation that looks correctly authorised | You omitted `x-service-token`, so the resolver fell through to the Privy path and sent your `mol_` credential to Privy | Send the service token. Do not change the credential. |
| Consumer credential seems ignored | You also sent `x-api-key` | Send `Authorization` alone. |
| `UNAUTHENTICATED` / `NONCE_EXPIRED` | More than 10 minutes between fetching the sign-in message and redeeming it | Fetch a **fresh** `getServiceSignInMessage` and sign again. Never retry the old signature. |
| `UNAUTHENTICATED` / `NONCE_NOT_FOUND` | The nonce was already redeemed, or a concurrent sign-in under the same `serviceName` overwrote it | Fetch a fresh message; use distinct service names for concurrent agents. |
| `UNAUTHENTICATED` / `INVALID_SIGNATURE` | The message was reformatted, rebuilt, or signed as typed data — or the wallet is a smart-contract account | Sign the returned `message` verbatim with `personal_sign`, from an EOA. |
| Token expires far sooner than `expiresAt` said | `expiresIn` used the `M` unit, which the signer reads as minutes | Re-issue with `s`/`m`/`h`/`d`/`w`, e.g. `"30d"`. |
| `NOT_FOUND` on a Lab the human can see in the app | You are pointed at the wrong environment | Check `MOLECULE_ENV` against the URL they used. |
| `UNAUTHORIZED` right after the grant | Indexer lag — role state has not propagated | Retry with backoff. Do **not** re-issue the token or re-grant. |
| `UNAUTHORIZED` that never clears | The wallet holds `VIEWER`, or the grant expired | Check `listLabMembers`; ask for `CONTRIBUTOR`. |
| `NOT_FOUND` / `PROJECT_NOT_FOUND` — "Project not found: 0x…" | Wrong `oclId`, or the Lab was created seconds ago and is not indexed. Arrives as a **thrown** query error, not in-band | Re-check the id character for character; retry with backoff only if the Lab was genuinely just created. |
| `UPSTREAM_UNAVAILABLE` / `MoleculeDataRoomPathOccupied` — "Path is occupied" | That `path` already exists | **Do not retry** — the code is on the retryable list but this is permanent. Use `ref: <datasetId>`, or a different `path`. |
| `INTERNAL_ERROR` with `details.reason: "UPLOAD_FINISH_ERROR"` on a finish that sent `encryptionMetadata` | Malformed encryption metadata — missing a required KMS field, or `accessControlConditions` that does not parse to an array | **Do not retry.** Validate the metadata locally, fix it, re-run from initiate. |
| Finish rejected for tags/categories | Wrong case, unknown tag, more than 3 tags, or a tag that does not belong to the category | Re-read `fileCategoriesAndTags` and retry the finish — the S3 upload is still valid. |
| Upload `PUT` returns 403 | Headers from initiate were altered, the presigned URL expired (check `uploadUrlExpiry`), or `contentLength` did not match the body — on the private path, the plaintext length was sent instead of the ciphertext length | Re-run initiate with the correct length and PUT with the exact returned headers. |
| `decryptDataKey` → `UNAUTHORIZED` with `details.reason: "UNAUTHORIZED"` | Gate 1: the membership index has not caught up with the grant | Wait and retry. Do not re-grant. |
| `decryptDataKey` → `UNAUTHORIZED` with `details.reason: "ACCESS_DENIED"` | Gate 2: the stored conditions do not admit this wallet — or they are malformed (wrong `chain` string, wrong resolver address, wrong `oclId`) | Check the `chain` string and resolver first. Retrying never helps. |
| `decryptDataKey` → `details.reason: "LEGACY_ENCRYPTION"` | The file predates this envelope format | Terminal. Report it; there is no workaround. |
| `decryptDataKey` → `details.reason: "MISSING_DEK"` | The stored `encryptionSystem` is a value the backend cannot unwrap | The file was finalised with a bad `encryptionSystem`. Terminal for that version. |
| `UNAUTHORIZED` / `details.reason: "PROTECTED_PATH"` on initiate or finish | The path is under the backend-owned `agreements/` folder | Choose a different path. Do not retry — this is not the indexer lag. |
| A finish starts failing with an agreement / `FAILED_PRECONDITION` error | The Lab's legal agreement gate has been enabled and the owner has not signed | The Contributor cannot resolve this. Tell the human it is theirs to do. |

## Revoking

The human revokes you with one on-chain call — `revokeRole(oclId, account)` — or by
letting the grant's `expiry` lapse. Either one also ends your ability to decrypt files you
encrypted yourself, so mention that when they set an expiry.

Independently, you can retire a token with `revokeServiceToken(tokenId)` — which needs the
`x-service-token` header, not just the id. Scoping is by the caller's wallet, not by the
token itself, so a token can extend or revoke any token issued by the same wallet. A
tokenId belonging to another wallet comes back as `NOT_FOUND`, exactly as a nonexistent
one does.

## Three addresses, not interchangeable

- **Your wallet** — `walletAddress` when issuing a token, the `did:ethr:` subject of
  `changeBy` when finishing an upload, and the identity both decrypt gates resolve.
- **The human owner's wallet** — theirs alone; you never need it and never ask for it.
- **The Lab's OCL account** (`labAccountAddress`) — the Lab's own on-chain account, and the
  second clause of the access conditions.

The `oclId` is none of these. Its trailing 40 hex characters *are* the OCL account address,
which is why they can be derived from each other — but they are not interchangeable as
arguments. `oclId` is a `bytes32` and goes in `hasRole`'s first parameter and every GraphQL
`oclId`; `labAccountAddress` is an `address` and goes in `isAuthorizedSignerForTba`'s second
parameter. Swap them and the ABI encoding throws inside the evaluator, which fails closed
and denies the *entire* condition array — including the branch that would have let the
owner in. The failure is total and silent.
