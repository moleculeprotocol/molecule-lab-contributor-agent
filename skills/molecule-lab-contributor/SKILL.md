---
name: molecule-lab-contributor
description: Work inside a Molecule Lab that someone else owns. Use when a human has created a Lab in the Molecule Labs app and wants their agent to upload files into it — the agent generates its own wallet, reports the address so the human can grant it the Contributor role, self-issues a service token, then reads and writes the Lab's data room. This lane owns no Lab, mints nothing, and spends nothing — no gas, no USDC, no x402. For creating or minting a new Lab, use a different skill.
license: Apache-2.0
---

# Molecule Lab Contributor

A human owns the Lab. You get your own identity, they grant it a role, and from then
on you authenticate as yourself. **You never receive the human's private key, token, or
session**, and they can revoke you with one click without touching anything else.

## What this skill is not

This is the **contributor** lane. You do not own a Lab and you never create one.

- Do **not** mint a LabNFT. Do **not** call `createLab`. If no Lab is named, ask for the
  `oclId` — never create one to proceed.
- You need **no funds**: no gas, no USDC, no x402 payment. Your key signs exactly one
  off-chain message and never sends a transaction. If you find yourself needing a funded
  wallet, you are on the wrong path — stop and re-read this file.
- Owner-only surfaces you cannot reach as a Contributor: `updateLabNftMetadata`,
  `generateLabImageUploadUrl`, and the legal-agreement mutations. Do not try them.

## Prerequisites

| | |
|---|---|
| **Consumer credential** | A `mol_<consumerId>_<secret>` string. The human supplies it. Treat the whole string as a secret. |
| **`oclId`** | The Lab's canonical 32-byte id, e.g. `0x0101…0042`. The human copies it from the Lab in the app. |
| **Node 18+** | For `fetch` and `viem`. Install viem with `npm i viem` in a scratch directory. |

Read all three from the environment. Pass them inline, or put them in a `.env` (see
`.env.example` in this repo) and load it with Node's built-in flag — no dependency:
`node --env-file=.env agent-upload.mjs ./file.csv`. Persist `AGENT_PRIVATE_KEY` there:
a new key on the next run is a different agent with no role on the Lab.

Never print the consumer credential, the service token, or the agent private key into
your reply. Read them from the environment.

## Constants

Default — **staging** (Base Sepolia). Use these unless the human explicitly says production:

```
GRAPHQL_URL   https://staging.graphql.api.molecule.xyz/graphql
LAB_APP_URL   https://testnet.labs.molecule.xyz
CHAIN         baseSepolia (84532)
```

Production (Base) — swap these two in, nothing else on this page changes:

```
GRAPHQL_URL   https://production.graphql.api.molecule.xyz/graphql
LAB_APP_URL   https://labs.molecule.xyz
CHAIN         base (8453)
```

## Headers

```
Content-Type:    application/json
Authorization:   mol_<consumerId>_<secret>     # NEVER prefixed with "Bearer"
X-Service-Token: <JWT>                         # mutations only; omit entirely until you have one
```

Three rules, each of which has broken a real run:

1. **No `Bearer`** in front of a `mol_` credential. `Bearer` is reserved for Privy user
   tokens; adding it fails authentication.
2. **Never send `x-api-key` as well.** The API's default auth mode is the shared API key,
   so a request carrying both headers is authenticated as the shared key and your
   consumer credential is ignored entirely. Send `Authorization` alone.
3. **Omit `X-Service-Token` rather than sending an empty one.** Public queries need only
   `Authorization`; an empty token header is worse than no header.

## Error contract

- **Queries throw.** Failure lands in top-level `errors[]`; branch on `errors[i].errorType`.
- **Mutations return errors in-band.** Every result type has `error: ApiError`.
  **Success ⇔ `error == null`** — never a truthy payload field, never a `message` string.
  Select `error { code message requestId retryable details }` on every mutation.
- **Parse `details` tolerantly.** It is an object on thrown query errors, a JSON string
  in-band, and currently a *doubly-encoded* JSON string in-band — one `JSON.parse` there
  returns a string and `.reason` is silently `undefined`. Loop until it is not a string.
- Branch on `code`, never on `message`. Retry only when `retryable` is true. Quote
  `requestId` in any bug report.
- Codes: `UNAUTHENTICATED`, `UNAUTHORIZED`, `NOT_FOUND`, `VALIDATION_FAILED`, `CONFLICT`,
  `FAILED_PRECONDITION`, `COMPLEXITY_LIMIT_EXCEEDED`, `RATE_LIMITED`\*, `TIMEOUT`\*,
  `UPSTREAM_UNAVAILABLE`\*, `INTERNAL_ERROR`\* (\* = retryable). Treat an unrecognised
  code as non-retryable, and surface it rather than swallowing it.

---

# The flow

| # | Actor | Action |
|---|---|---|
| 1 | Agent | Generate a wallet, report the address |
| 2 | **Human** | Add that address to the Lab as **Contributor** |
| 3 | Agent | Poll until the grant is visible |
| 4 | Agent | Self-issue a service token |
| 5 | Agent | Upload |
| 6 | Agent | Verify, and give the human a link |

**Run the phases in this order.** Phase 3 must complete before phase 4 — see the note on
the nonce there. It is the single most common way this flow fails.

## Phase 1 — Generate the agent wallet

```javascript
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

if (!process.env.AGENT_PRIVATE_KEY) {
  // Generate ONCE, hand the key to the human to store, then STOP. Do not carry on:
  // a wallet nobody has granted a role to can never get past phase 3.
  console.log("AGENT_PRIVATE_KEY=" + generatePrivateKey());
  throw new Error("Store that key, get the role granted for its address, then re-run.");
}
const agentAccount = privateKeyToAccount(process.env.AGENT_PRIVATE_KEY);
console.log("Agent wallet address:", agentAccount.address);
```

**Persist the key.** If `AGENT_PRIVATE_KEY` is not already set, write the generated key
somewhere durable (an `.env` the human controls, or their secret store) and tell them to
keep it. A new key on the next run is a *different agent* with no role, and the human
would have to grant the role again.

Print the **address** only. Never print the private key into your reply or into a file
you also show. This key is yours, not the human's — it is not their wallet and must
never be asked for.

## Phase 2 — Report the address, then stop

Tell the human, in plain terms:

> My wallet address is `0x…`. In the Labs app, open your Lab → Members → add this
> address with the **Contributor** role. Tick "is agent" and set an expiry that matches
> how long you want me working on this. Tell me when it's done.

Then **stop and wait**. Do not poll silently for minutes with no output, and do not try
to grant the role yourself — only the Lab **Owner** can grant Contributor.

Why Contributor and not Viewer: a Viewer can read and decrypt but cannot write.
Uploading requires Contributor.

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
  expired grants are excluded from this list entirely). Keep polling, then ask.

`isAgent` merely echoes the flag the owner set; `false` there changes nothing about what
you may do, so do not treat it as a failed grant. `expiry` is unix seconds as a decimal
string, or `null` for a permanent grant.

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

```javascript
const messageSignature = await agentAccount.signMessage({
  message: signIn.getServiceSignInMessage.message,
});
```

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

- `serviceName` is free-form. Use something identifying, e.g. `"research-agent-1"`.
- `expiresIn` format is `<int><unit>`, unit one of `s m h d w M y`, bounds 1 hour to
  2 years. The backend default is `180d`; prefer `"30d"`, or match the role grant's expiry.
- The token goes in `X-Service-Token` on everything from here on. **It is wallet-bound,
  not Lab-bound** — authorisation is resolved per request from your wallet's role on the
  Lab you name, so one token works across every Lab you hold a role on.

Issuance is **not** gated on holding a role — any wallet can mint a token for itself. The
role is what makes the token *useful*. A token issued before the grant lands keeps
working once it does; you never need to re-issue it because of a permissions error.

## Phase 5 — Upload

Three calls. Wrap the whole sequence in the retry helper below.

**5a — initiate:**

```graphql
mutation Initiate($oclId: String!, $contentType: String!, $contentLength: Int!) {
  initiateCreateOrUpdateFile(oclId: $oclId, contentType: $contentType, contentLength: $contentLength) {
    uploadToken uploadUrl uploadUrlExpiry method headers { key value }
    error { code message requestId retryable details }
  }
}
```

**5b — PUT the raw bytes** to `uploadUrl` using the returned `method` and **exactly** the
returned `headers`, converted from the `[{key,value}]` array to an object. Do not add,
drop, or reorder headers. Presigned URLs expire in ~15 minutes. This call goes to S3, not
to the API — send no `Authorization` and no `X-Service-Token`.

**5c — finish:**

```graphql
mutation Finish(
  $oclId: String!, $uploadToken: String!, $path: String!, $accessLevel: String!,
  $changeBy: String!, $description: String, $tags: [String!], $categories: [String!],
  $contentText: String
) {
  finishCreateOrUpdateFile(
    oclId: $oclId, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel,
    changeBy: $changeBy, description: $description, tags: $tags, categories: $categories,
    contentText: $contentText
  ) {
    datasetId contentHash version
    error { code message requestId retryable details }
  }
}
```

- `contentType` in 5a is **stored and shown to the human** — send the file's real MIME
  type (`text/csv`, `application/pdf`, `image/png`). `application/octet-stream` is
  accepted but leaves the file looking like an unidentified blob in the data room.
- `accessLevel`: `"PUBLIC"` | `"HOLDERS"` | `"ADMIN"`.
- `changeBy`: **your** wallet address — the file is attributed to you.
- `path` for a **new** file; `ref` (a `datasetId`) for a **new version** of an existing
  file. One or the other, never both. Underscores are fine — some older reference docs
  say otherwise, but the API accepts them (verified against staging).
- **Re-using an existing `path` fails.** A second upload to the same path returns
  `UPSTREAM_UNAVAILABLE` / `MoleculeDataRoomPathOccupied` — "Path is occupied". Despite
  that code sitting on the retryable list, **this is permanent**: retrying can never
  succeed. To add a new version of a file, pass `ref` (its `datasetId`) instead of
  `path`; otherwise choose a different path.
- The stored path comes back **with a leading `/`** — you send `findings.csv` and
  `dataRoom.files` reports `/findings.csv`. Match with `endsWith`, not `===`.
- `categories` / `tags` are optional. If you set them, take valid values from the public
  `fileCategoriesAndTags` query rather than inventing them.
- `contentText` is optional searchable text, used for semantic search.

### Retry `UNAUTHORIZED` right after the grant

Role state reaches the API through an event indexer, so for a window after the human's
grant confirms on-chain a write still returns `UNAUTHORIZED`. **This is not a permissions
problem.** Re-issuing the token will not help, and asking the human to re-grant will not
help. Wait and retry. Usually seconds; it has taken minutes.

```javascript
async function withIndexerLagRetry(fn, { codes = ["UNAUTHORIZED", "NOT_FOUND"], attempts = 12, baseMs = 2000, capMs = 30000 } = {}) {
  const laggy = new RegExp(codes.join("|"));
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      if (!laggy.test(String(err)) || i === attempts - 1) throw err;
      const delay = Math.min(baseMs * 2 ** i, capMs);   // 2s, 4s, 8s, 16s, then 30s
      console.warn(`indexer not caught up (attempt ${i + 1}/${attempts}); retrying in ${delay / 1000}s`);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
}
```

## Phase 6 — Verify and hand back

```graphql
query Verify($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    oclId shortname name
    dataRoom { files { path contentType accessLevel version createdBy } }
  }
}
```

Public query, `Authorization` only. Confirm your `path` is in `dataRoom.files` and that
`createdBy` matches your address. A `null` result means the Lab is not registered — this
query is nullable and does not throw for a missing Lab.

Then give the human the link: `<LAB_APP_URL>/projects/<shortname>`. They will see the
file in the data room, attributed to your address, flagged as an agent in the members list.

## Reading the data room

A Contributor can read as well as write — useful when the job is "analyse what's already
in the Lab, then write the results back". Public query, `Authorization` only:

```graphql
query ReadDataRoom($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    shortname
    dataRoom {
      files {
        path contentType contentHash version createdBy
        downloadUrl downloadHeaders { key value } downloadUrlExpiry
      }
    }
  }
}
```

`GET` the `downloadUrl` with exactly the returned `downloadHeaders` to fetch the bytes.
The URL is presigned and short-lived — read `downloadUrlExpiry` and re-run the query
rather than caching the URL.

```javascript
const headers = {};
(file.downloadHeaders ?? []).forEach((h) => (headers[h.key] = h.value));
const body = await (await fetch(file.downloadUrl, { headers })).text();
```

`contentHash` lets you confirm the bytes are intact. Note it is **not** a bare SHA-256 of
the content — it is the data room's own multihash-style digest, so compare it between
reads rather than recomputing it locally.

Files with a non-`PUBLIC` `accessLevel` are encrypted at rest and need the key-management
flow, which this skill does not cover — their `downloadUrl` yields ciphertext.

---

## Complete script

Self-contained. Run it with the agent's own key and the `oclId` of the human's Lab.

```javascript
#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { basename, extname } from "node:path";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

// The stored contentType is what the human sees in the data room — send a real one.
const MIME = {
  ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json",
  ".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf",
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};
const mimeFor = (f) => MIME[extname(f).toLowerCase()] ?? "application/octet-stream";

const GRAPHQL_URL = process.env.GRAPHQL_URL ?? "https://staging.graphql.api.molecule.xyz/graphql";
const LAB_APP_URL = process.env.LAB_APP_URL ?? "https://testnet.labs.molecule.xyz";
const SERVICE_NAME = process.env.SERVICE_NAME ?? "research-agent-1";

const CONSUMER_CREDENTIAL = process.env.CONSUMER_CREDENTIAL;
const OCL_ID = process.env.OCL_ID;
const AGENT_PRIVATE_KEY = process.env.AGENT_PRIVATE_KEY;

let serviceToken;

async function graphql(query, variables) {
  // Authorization ONLY — never also x-api-key, or the consumer credential is ignored.
  const headers = { "Content-Type": "application/json", Authorization: CONSUMER_CREDENTIAL };
  if (serviceToken) headers["X-Service-Token"] = serviceToken;
  const res = await fetch(GRAPHQL_URL, { method: "POST", headers, body: JSON.stringify({ query, variables }) });
  const { data, errors } = await res.json();
  if (errors) throw new Error(JSON.stringify(errors));
  return data;
}

// `details` is an object on thrown query errors, a JSON string in-band, and currently a
// doubly-encoded JSON string in-band. Parse until it stops being a string.
function parseDetails(details) {
  let value = details;
  for (let i = 0; i < 3 && typeof value === "string"; i++) {
    try { value = JSON.parse(value); } catch { break; }
  }
  return value && typeof value === "object" ? value : {};
}

function assertOk(result, op) {
  if (result.error) {
    const { code, message, requestId } = result.error;
    const { reason } = parseDetails(result.error.details);
    throw new Error(`${op} failed: ${code}${reason ? `/${reason}` : ""}: ${message} (requestId ${requestId})`);
  }
  return result;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function withIndexerLagRetry(fn, { codes = ["UNAUTHORIZED", "NOT_FOUND"], attempts = 12, baseMs = 2000, capMs = 30000 } = {}) {
  const laggy = new RegExp(codes.join("|"));
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      if (!laggy.test(String(err)) || i === attempts - 1) throw err;
      const delay = Math.min(baseMs * 2 ** i, capMs);
      console.warn(`indexer not caught up (attempt ${i + 1}/${attempts}); retrying in ${delay / 1000}s`);
      await sleep(delay);
    }
  }
}

async function main() {
  const filePath = process.argv[2];
  if (!filePath) throw new Error("Usage: node agent-upload.mjs <file-to-upload>");
  if (!CONSUMER_CREDENTIAL) throw new Error("Set CONSUMER_CREDENTIAL to your mol_ credential");
  if (!OCL_ID) throw new Error("Set OCL_ID to the oclId of the lab the human owns");

  // ---- Phase 1: the agent's identity ----
  if (!AGENT_PRIVATE_KEY) {
    console.log("No AGENT_PRIVATE_KEY set. Generated one for this run only — store it, or");
    console.log("the next run is a different agent with no role:\n");
    console.log("  AGENT_PRIVATE_KEY=" + generatePrivateKey() + "\n");
    throw new Error("Store that key, have the owner grant it Contributor, then re-run.");
  }
  const agentAccount = privateKeyToAccount(AGENT_PRIVATE_KEY);
  console.log("1/6 Agent wallet:", agentAccount.address);

  // ---- Phase 2/3: wait for the human's grant (public query, no service token) ----
  // Poll first and only prompt on a miss — the human is often already ahead of you.
  let grant, asked = false;
  for (let i = 0; i < 60; i++) {
    const members = await graphql(
      `query ListLabMembers($oclId: String!) {
        listLabMembers(oclId: $oclId) { members { walletAddress role isAgent expiry } }
      }`,
      { oclId: OCL_ID },
    );
    grant = members.listLabMembers.members.find(
      (m) => m.walletAddress.toLowerCase() === agentAccount.address.toLowerCase(),
    );
    if (grant) break;
    if (!asked) {
      console.log("2/6 Ask the lab owner to add that address as Contributor (isAgent = true).");
      asked = true;
    }
    await sleep(5000); // poll for up to 5 minutes
  }
  if (!grant) throw new Error("No role grant found for the agent wallet — ask the owner to add it");
  if (grant.role === "VIEWER") throw new Error("Agent holds VIEWER; uploading needs CONTRIBUTOR");
  console.log("3/6 Role:", grant.role, "expiry:", grant.expiry ?? "permanent");

  // ---- Phase 4: self-issue a token ----
  // Only now, after the poll returned. The sign-in message carries a single-use nonce
  // that expires 10 minutes after issuance.
  const signIn = await graphql(
    `query GetServiceSignInMessage($walletAddress: String!, $serviceName: String!) {
      getServiceSignInMessage(walletAddress: $walletAddress, serviceName: $serviceName) { message expiresAt }
    }`,
    { walletAddress: agentAccount.address, serviceName: SERVICE_NAME },
  );
  const messageSignature = await agentAccount.signMessage({
    message: signIn.getServiceSignInMessage.message, // verbatim — never rebuild this string
  });
  const tokenResult = await graphql(
    `mutation GenerateServiceToken($serviceName: String!, $walletAddress: String!, $messageSignature: String!, $expiresIn: String) {
      generateServiceToken(serviceName: $serviceName, walletAddress: $walletAddress, messageSignature: $messageSignature, expiresIn: $expiresIn) {
        token expiresAt
        error { code message requestId retryable details }
      }
    }`,
    { serviceName: SERVICE_NAME, walletAddress: agentAccount.address, messageSignature, expiresIn: "30d" },
  );
  assertOk(tokenResult.generateServiceToken, "generateServiceToken");
  serviceToken = tokenResult.generateServiceToken.token;
  console.log("4/6 Token issued, expires", tokenResult.generateServiceToken.expiresAt);

  // ---- Phase 5: upload ----
  const bytes = readFileSync(filePath);
  const { datasetId } = await withIndexerLagRetry(async () => {
    const initiated = await graphql(
      `mutation Initiate($oclId: String!, $contentType: String!, $contentLength: Int!) {
        initiateCreateOrUpdateFile(oclId: $oclId, contentType: $contentType, contentLength: $contentLength) {
          uploadToken uploadUrl method headers { key value }
          error { code message requestId retryable details }
        }
      }`,
      { oclId: OCL_ID, contentType: mimeFor(filePath), contentLength: bytes.length },
    );
    assertOk(initiated.initiateCreateOrUpdateFile, "initiateCreateOrUpdateFile");
    const { uploadToken, uploadUrl, method, headers } = initiated.initiateCreateOrUpdateFile;

    const uploadHeaders = {};
    headers.forEach((h) => (uploadHeaders[h.key] = h.value));
    const put = await fetch(uploadUrl, { method: method || "PUT", headers: uploadHeaders, body: bytes });
    if (!put.ok) throw new Error(`Upload failed: ${put.status}`);

    const finished = await graphql(
      `mutation Finish($oclId: String!, $uploadToken: String!, $path: String!, $accessLevel: String!, $changeBy: String!) {
        finishCreateOrUpdateFile(oclId: $oclId, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel, changeBy: $changeBy) {
          datasetId
          error { code message requestId retryable details }
        }
      }`,
      {
        oclId: OCL_ID,
        uploadToken,
        path: basename(filePath),
        accessLevel: "PUBLIC",
        changeBy: agentAccount.address,
      },
    );
    assertOk(finished.finishCreateOrUpdateFile, "finishCreateOrUpdateFile");
    return finished.finishCreateOrUpdateFile;
  });
  console.log("5/6 Uploaded — datasetId:", datasetId);

  // ---- Phase 6: verify ----
  const verify = await graphql(
    `query Verify($oclId: String!) {
      labWithDataRoomAndFiles(oclId: $oclId) {
        shortname
        dataRoom { files { path accessLevel version createdBy } }
      }
    }`,
    { oclId: OCL_ID },
  );
  const lab = verify.labWithDataRoomAndFiles;
  // Nullable by design: null means the lab is not registered, and the query does not throw.
  if (!lab) throw new Error(`Lab ${OCL_ID} is not registered — check the oclId`);
  // Stored paths carry a leading slash, so match on endsWith rather than equality.
  const file = lab.dataRoom.files.find((f) => f.path.endsWith(basename(filePath)));
  if (!file) throw new Error("File not found in the data room");
  const mine = file.createdBy?.toLowerCase() === agentAccount.address.toLowerCase();
  console.log("6/6 Verified:", file.path, file.accessLevel, mine ? "— attributed to the agent" : `— createdBy: ${file.createdBy}`);
  if (lab.shortname) {
    console.log("Human can see it at:", `${LAB_APP_URL}/projects/${lab.shortname}`);
  }
}

main().catch((err) => { console.error(err); process.exit(1); });
```

**Usage:**

```bash
npm i viem

# First run — prints a generated agent key, then stops so the owner can grant the role
CONSUMER_CREDENTIAL="mol_…" OCL_ID="0x0101…" node agent-upload.mjs ./findings.csv

# Subsequent runs, once the owner has granted Contributor to that address
AGENT_PRIVATE_KEY="0x…" CONSUMER_CREDENTIAL="mol_…" OCL_ID="0x0101…" \
  node agent-upload.mjs ./findings.csv
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `UNAUTHENTICATED` on any call | `Bearer` in front of the `mol_` credential, or the credential is wrong/expired | Send the credential bare. Ask the human to check it. |
| Consumer credential seems ignored | You also sent `x-api-key` | Send `Authorization` alone. |
| `UNAUTHENTICATED` / `NONCE_EXPIRED` | More than 10 minutes between fetching the sign-in message and redeeming it | Fetch a **fresh** `getServiceSignInMessage` and sign again. Never retry the old signature. |
| `UNAUTHENTICATED` / `NONCE_NOT_FOUND` | The nonce was already redeemed, or you never called `getServiceSignInMessage` | Fetch a fresh message. |
| `UNAUTHENTICATED` / `INVALID_SIGNATURE` | The message was reformatted, rebuilt, or signed as typed data | Sign the returned `message` verbatim with `personal_sign`. |
| `UNAUTHORIZED` right after the grant | Indexer lag — role state has not propagated | Retry with backoff. Do **not** re-issue the token or re-grant. |
| `UNAUTHORIZED` that never clears | The wallet holds `VIEWER`, or the grant expired | Check `listLabMembers`; ask for `CONTRIBUTOR`. |
| `NOT_FOUND` / `PROJECT_NOT_FOUND` — "Project not found: 0x…" | Wrong `oclId`, or the Lab was created seconds ago and is not indexed. Surfaces at the phase-3 members poll, before any upload, and arrives as a **thrown** query error rather than in-band | Re-check the id character for character; retry with backoff only if the Lab was genuinely just created. |
| `UPSTREAM_UNAVAILABLE` / `MoleculeDataRoomPathOccupied` — "Path is occupied" | That `path` already exists in the data room (e.g. you re-ran the same upload) | **Do not retry** — the code is on the retryable list but this condition is permanent. Use `ref: <datasetId>` for a new version, or a different `path`. |
| Upload `PUT` returns 403 | Headers from `initiate` were altered, or the presigned URL expired (~15 min) | Re-run `initiate` and PUT with the exact returned headers. |

## Revoking

The human revokes you with one on-chain call — `revokeRole(oclId, account)` — or by
letting the grant's `expiry` lapse. Independently, you can retire your own token with
`revokeServiceToken(tokenId)`; a token may only extend or revoke **its own** record.

## Three addresses, not interchangeable

- **Your wallet** — `walletAddress` when issuing a token, `changeBy` when finishing an upload.
- **The human owner's wallet** — theirs alone; you never need it and never ask for it.
- **The Lab's OCL account** (`labAccountAddress`) — the Lab's own on-chain account.

The `oclId` is none of these. Its trailing 40 hex characters happen to be the OCL account
address, which makes it look interchangeable. It is not.
