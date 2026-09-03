#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0",
#   "httpx>=0.27",
#   "cryptography>=42",
#   "eth-account>=0.13.7",
# ]
#
# [tool.uv]
# # Never compile a dependency from source. eth-account pulls C/Rust extensions and
# # cryptography has a Rust core; the newest release of some of those ships arm64-only
# # macOS wheels, so without this an Intel Mac drops to a source build that needs a
# # compiler and a Rust toolchain. With it, uv resolves to the newest version that HAS a
# # wheel for this machine, and says so plainly if none exists.
# no-build = true
# ///
"""mol-labs-mcp — stdio MCP server for the molecule-lab-contributor skill.

Everything an agent needs to work inside a Molecule Lab somebody else owns, and nothing
else: no minting, no payments, no gas. The agent holds its own key, the Lab owner grants it
a role, it issues its own token, and it reads and writes one data room.

Two properties are enforced here rather than left to prose, because prose is what failed:

1. **The human confirms the plan.** `stage_upload` records exactly what is about to happen
   and hands it back to be read out. `confirm_upload_plan` records the human's own words.
   Every tool that writes refuses until that has happened, and refuses if the details drift.
   "The user agreed" becomes a checked precondition rather than an assumption.

2. **Confidential means confidential.** Once a file is encrypted for a private upload this
   process will not upload its plaintext, and will not finalize it as public or without
   encryption metadata — whatever it is later asked to do.

Secrets stay out of tool results: the plaintext data key never leaves this process, and
every outgoing message goes through a redactor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
# The SDK renamed FastMCP to MCPServer in 2.x. Both expose the same .tool()/.run(), so
# accept either rather than pinning users to one major version.
try:
    from mcp.server.mcpserver import MCPServer as _McpServer  # mcp >= 2
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

mcp = _McpServer("mol-labs")

_http = httpx.Client(timeout=120.0, follow_redirects=True)


# ==========================================================================
# diagnostics, errors, redaction
# ==========================================================================


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class ToolError(Exception):
    """A tool error whose message is safe to show the agent and the human."""


# Deliberately NOT a blanket 0x+64-hex rule: an OCL id has exactly that shape, is public,
# and is the single most important value these tools return — blanking it would break the
# flow to guard against something that never appears here. A private key reaches this
# process only from the environment, and no tool returns one (agent_wallet returns the
# address alone), so the key patterns below cover the forms that could realistically leak:
# a credential, a token, or a key that arrived as a KEY=value assignment.
_SECRET_PATTERNS = (
    re.compile(r"mol_[A-Za-z0-9_\-]{4,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)((?:PRIVATE_KEY|SECRET|PASSWORD)\s*[=:]\s*)\S+"),
)


def redact(text: Any) -> str:
    """Blank out anything credential-shaped. Applied to everything that leaves here."""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda m: (m.group(1) + "<redacted>") if m.groups() else "<redacted>", out
        )
    return out


def dump(obj: Any) -> str:
    return redact(json.dumps(obj, indent=2, default=str))


# ==========================================================================
# configuration
#
# A stdio MCP server sees only the environment its host injects, so this also reads the
# `env` block of the nearest project .claude/settings.json and settings.local.json. The
# real process environment always wins, nothing is overwritten, and no value is logged.
#
# This happens once, at spawn. Editing configuration mid-session changes nothing until the
# host restarts the server — in Claude Code, reconnect with /mcp.
# ==========================================================================

_ENV_SOURCES: dict[str, str] = {}
_CONFIG_BASE: str | None = None


def env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _settings_env(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"[config] skipping unreadable {path}: {exc}")
        return {}
    block = data.get("env") if isinstance(data, dict) else None
    if isinstance(block, dict):
        return {k: str(v) for k, v in block.items() if v not in (None, "")}
    return {}


def _load_dotenv(path: Path) -> dict[str, str]:
    """Read a KEY=VALUE .env. No dependency, no interpolation, quotes stripped."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            out[key] = value
    return out


def _bootstrap_env() -> None:
    global _CONFIG_BASE
    starts: list[Path] = []
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        starts.append(Path(os.environ["CLAUDE_PLUGIN_ROOT"]))
    for getter in (lambda: Path(__file__).resolve().parent, Path.cwd):
        try:
            starts.append(getter())
        except Exception:
            pass
    try:
        home_claude = (Path.home() / ".claude").resolve()
    except Exception:
        home_claude = None

    # .env files first — nearest wins, and neither ever overwrites the real environment.
    seen_dirs: set[Path] = set()
    for start in starts:
        try:
            chain = [start.resolve(), *start.resolve().parents]
        except Exception:
            continue
        for directory in chain:
            if directory in seen_dirs:
                continue
            seen_dirs.add(directory)
            dotenv = directory / ".env"
            if not dotenv.is_file():
                continue
            filled = []
            for key, value in _load_dotenv(dotenv).items():
                if key in os.environ:
                    continue
                os.environ[key] = value
                _ENV_SOURCES[key] = str(dotenv)
                filled.append(key)
            if filled:
                log(f"[config] {dotenv}: loaded {len(filled)} var(s) {sorted(filled)}")

    seen: set[Path] = set()
    candidates: list[Path] = []
    for start in starts:
        try:
            chain = [start.resolve(), *start.resolve().parents]
        except Exception:
            continue
        for directory in chain:
            claude = directory / ".claude"
            try:
                resolved = claude.resolve()
            except Exception:
                continue
            if resolved in seen or resolved == home_claude:
                continue
            try:
                if not claude.is_dir():
                    continue
            except OSError:
                continue
            seen.add(resolved)
            candidates.append(claude)
    candidates.sort(key=lambda p: len(p.parts), reverse=True)  # nearest first

    for claude in candidates:
        local = _settings_env(claude / "settings.local.json")
        shared = _settings_env(claude / "settings.json")
        if not local and not shared:
            continue
        _CONFIG_BASE = str(claude)
        filled = []
        for name, block in (("settings.local.json", local), ("settings.json", shared)):
            for key, value in block.items():
                if key in os.environ:
                    continue
                os.environ[key] = value
                _ENV_SOURCES[key] = str(claude / name)
                filled.append(key)
        log(f"[config] base {claude}: loaded {len(filled)} var(s) {sorted(filled)}")
        return


_bootstrap_env()


# ==========================================================================
# endpoints and chain
#
# The defaults are the live Molecule Labs API — the only thing an ordinary user needs. Each
# one can be overridden by an environment variable, which is how the Molecule team points
# an agent at a different deployment for testing: set the variables in .env and nothing
# else about the flow changes.
# ==========================================================================

DEFAULTS = {
    "MOLECULE_LABS_URL": "https://production.graphql.api.molecule.xyz/graphql",
    "MOLECULE_CLIENT_URL": "https://labs.molecule.xyz",
    "MOLECULE_CHAIN_ID": "8453",
    "MOLECULE_ACCESS_RESOLVER": "0x89a14Be8f7824d4775053Edad0f2fA2d6767b72B",
}

# Chain names the API's condition evaluator accepts, keyed by chain id. It accepts a fixed
# list and is fail-closed, so an unrecognised value denies the whole condition array and a
# file would upload fine and never decrypt. Derived from the chain id rather than
# configured separately, so the two cannot drift apart.
_CONDITION_CHAIN_BY_ID = {1: "ethereum", 8453: "base", 84532: "sepolia-base", 11155111: "sepolia"}
_CHAIN_NAME_BY_ID = {1: "Ethereum", 8453: "Base", 84532: "Base Sepolia", 11155111: "Sepolia"}
_DEFAULT_RPC_BY_ID = {8453: "https://mainnet.base.org", 84532: "https://sepolia.base.org"}


def setting(name: str) -> str:
    value = env(name) or DEFAULTS.get(name)
    if not value:
        raise ToolError(f"{name} is not set and has no default.")
    return value


def chain_id() -> int:
    raw = setting("MOLECULE_CHAIN_ID")
    try:
        return int(raw)
    except ValueError:
        raise ToolError(f"MOLECULE_CHAIN_ID must be a number, got {raw!r}.") from None


def condition_chain() -> str:
    cid = chain_id()
    name = _CONDITION_CHAIN_BY_ID.get(cid)
    if not name:
        raise ToolError(
            f"No access-condition chain name is known for chain id {cid}. The API accepts only "
            f"a fixed list, and an unknown value would make every private file permanently "
            f"undecryptable."
        )
    return name


def rpc_url() -> str:
    url = env("EVM_RPC_URL") or _DEFAULT_RPC_BY_ID.get(chain_id())
    if not url:
        raise ToolError(f"No RPC endpoint known for chain id {chain_id()}. Set EVM_RPC_URL.")
    return url


def endpoints() -> dict[str, Any]:
    cid = chain_id()
    return {
        "labsApi": setting("MOLECULE_LABS_URL"),
        "labsApp": setting("MOLECULE_CLIENT_URL").rstrip("/"),
        "chainId": cid,
        "chainName": _CHAIN_NAME_BY_ID.get(cid, f"chain {cid}"),
        "accessResolver": setting("MOLECULE_ACCESS_RESOLVER"),
        "conditionChain": condition_chain(),
        "rpcUrl": rpc_url(),
    }


# Access-resolver roles. The contract accepts only these two; 0 and 3+ revert, and a revert
# denies the entire condition array.
ROLE_VIEWER = 1
ROLE_CONTRIBUTOR = 2
_ROLE_BY_NAME = {"viewer": ROLE_VIEWER, "contributor": ROLE_CONTRIBUTOR}

# The stored content type is what the human sees in the data room. There is deliberately no
# mimetypes.guess_type fallback: it returns plausible-but-wrong types for unknown
# extensions, and this flow would rather stop and ask than mislabel someone's file.
MIME = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".parquet": "application/vnd.apache.parquet",
}


# ==========================================================================
# .env upkeep — the agent writes the secrets it is handed or creates
# ==========================================================================


def upsert_env_file(env_file: str, key: str, value: str) -> str:
    """Set KEY=value in a .env, replacing any existing line for that key.

    Creates the file at mode 0600 if absent and tightens permissions if present — a file
    holding a private key should not be world-readable.
    """
    path = Path(env_file).expanduser()
    lines: list[str] = []
    replaced = False
    if path.exists():
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if re.match(rf"^\s*(export\s+)?{re.escape(key)}\s*=", line):
                lines[i] = f"{key}={value}"
                replaced = True
                break
    if not replaced:
        lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    path.write_text("\n".join(lines).rstrip("\n") + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ[key] = value  # usable immediately, without a restart
    return "replaced" if replaced else "appended"


# ==========================================================================
# in-process session state
# ==========================================================================

# Plaintext data keys behind opaque handles, so they never enter the conversation.
_DEK_VAULT: dict[str, tuple[str, float]] = {}

# Fail-closed latches. Process-local, no override, cleared only by a server restart.
_CONFIDENTIAL_PLAINTEXT_HASHES: set[str] = set()
_CONFIDENTIAL_PLAINTEXT_PATHS: set[str] = set()
_CONFIDENTIAL_LABS: set[str] = set()

# The staged, human-approved plan. One at a time, deliberately.
_PLAN: dict[str, Any] | None = None


def put_dek(plaintext_dek: str) -> str:
    handle = f"dek_{uuid.uuid4().hex[:16]}"
    _DEK_VAULT[handle] = (plaintext_dek, time.time() + 3600)
    return handle


def get_dek(handle: str) -> str:
    entry = _DEK_VAULT.get(handle)
    if not entry:
        raise ToolError("Unknown data-key handle. Generate a fresh one.")
    value, expires = entry
    if time.time() > expires:
        _DEK_VAULT.pop(handle, None)
        raise ToolError("That data-key handle has expired. Generate a fresh one.")
    return value


def mark_confidential(file_path: str, plaintext_sha256: str) -> None:
    _CONFIDENTIAL_PLAINTEXT_HASHES.add(plaintext_sha256.lower())
    try:
        _CONFIDENTIAL_PLAINTEXT_PATHS.add(str(Path(file_path).resolve()))
    except OSError:
        pass


def mark_confidential_lab(ocl_id: str) -> None:
    if ocl_id and ocl_id.strip():
        _CONFIDENTIAL_LABS.add(ocl_id.strip().lower())


def assert_not_confidential_plaintext(file_path: str, data: bytes) -> None:
    """Refuse to upload the plaintext of a file encrypted for a private upload.

    Keyed on both the resolved path and the bytes, so a copy at another path is caught too.
    """
    digest = hashlib.sha256(data).hexdigest().lower()
    try:
        resolved = str(Path(file_path).resolve())
    except OSError:
        resolved = None
    if digest in _CONFIDENTIAL_PLAINTEXT_HASHES or (
        resolved and resolved in _CONFIDENTIAL_PLAINTEXT_PATHS
    ):
        raise ToolError(
            "PRIVACY GUARD (not overridable): refusing to upload the plaintext of a file that "
            "was encrypted for a confidential upload. A private upload must never fall back to "
            "a public one. Upload the ciphertext, or abort and report the failure — never "
            "publish this file's plaintext."
        )


def assert_confidential_finalize_ok(ocl_id: str, access_level: str, has_metadata: bool) -> None:
    if (ocl_id or "").strip().lower() not in _CONFIDENTIAL_LABS:
        return
    if access_level.upper() == "PUBLIC" or not has_metadata:
        raise ToolError(
            f"PRIVACY GUARD (not overridable): refusing to finalize a file for this Lab with "
            f"access level {access_level or 'MISSING'} and encryption metadata "
            f"{'present' if has_metadata else 'MISSING'}. Access conditions were built for this "
            f"Lab, so its file must be finalized with a non-public access level AND encryption "
            f"metadata. Do not fall back to the public path — abort and report the failure."
        )


# ==========================================================================
# AES-256-GCM envelope — the data-room format, matching the Labs app exactly:
#   random 12-byte IV | 128-bit tag APPENDED to the ciphertext | no additional data
#   key and iv are standard padded base64 | content hash is hex SHA-256 of the PLAINTEXT
#
# The content-hash rule is the one that catches people: the schema's own description for
# that field says "hash of the encrypted content", which is wrong, and nothing on the
# server checks the value — so getting it wrong is silent.
# ==========================================================================

TAG_BYTES = 16
IV_BYTES = 12


def _key_bytes(plaintext_dek: str) -> bytes:
    key = base64.b64decode(plaintext_dek)
    if len(key) != 32:
        raise ToolError(f"The data key must decode to 32 bytes (AES-256), got {len(key)}.")
    return key


def encrypt_bytes(plaintext: bytes, plaintext_dek: str) -> dict[str, Any]:
    key = _key_bytes(plaintext_dek)
    iv = secrets.token_bytes(IV_BYTES)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)  # ciphertext || 16-byte tag
    return {
        "ciphertext": ciphertext,
        "iv": base64.b64encode(iv).decode(),
        "contentHash": hashlib.sha256(plaintext).hexdigest(),
        "cipherBytes": len(ciphertext),
    }


def decrypt_bytes(ciphertext: bytes, iv: str, plaintext_dek: str) -> bytes:
    key = _key_bytes(plaintext_dek)
    if len(ciphertext) < TAG_BYTES:
        raise ToolError("Ciphertext is shorter than the GCM auth tag.")
    return AESGCM(key).decrypt(base64.b64decode(iv), bytes(ciphertext), None)


# ==========================================================================
# access control conditions — the lock on a private file
#
# Nothing validates this array at upload time: not that it names this Lab, not that the
# resolver exists, not that the chain name is one the evaluator knows. And evaluation is
# fail-closed, so any error denies the WHOLE array — a broken first clause takes the
# owner's clause down with it. Emit what the Labs app emits, and check it before sending.
# ==========================================================================


def _has_role_condition(chain: str, resolver: str, ocl_id: str, role: int) -> dict:
    return {
        "chain": chain,
        "conditionType": "evmContract",
        "contractAddress": resolver,
        "functionName": "hasRole",
        # functionParams are STRINGS, the role included. An int here diverges from every
        # other client.
        "functionParams": [ocl_id, ":userAddress", str(role)],
        "functionAbi": {
            "name": "hasRole",
            "inputs": [
                {"internalType": "bytes32", "name": "oclId", "type": "bytes32"},
                {"internalType": "address", "name": "account", "type": "address"},
                {"internalType": "uint8", "name": "role", "type": "uint8"},
            ],
            "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        },
        "returnValueTest": {"comparator": "=", "key": "", "value": "true"},
    }


def _tba_owner_condition(chain: str, resolver: str, lab_account: str) -> dict:
    return {
        "chain": chain,
        "conditionType": "evmContract",
        "contractAddress": resolver,
        "functionName": "isAuthorizedSignerForTba",
        "functionParams": [":userAddress", lab_account],
        "functionAbi": {
            "name": "isAuthorizedSignerForTba",
            "inputs": [
                {"internalType": "address", "name": "signer", "type": "address"},
                {"internalType": "address", "name": "account", "type": "address"},
            ],
            "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        },
        "returnValueTest": {"comparator": "=", "key": "", "value": "true"},
    }


def lab_conditions(ocl_id: str, lab_account: str, role: int) -> list[dict]:
    """hasRole(oclId, caller, role) OR isAuthorizedSignerForTba(caller, labAccount).

    The second clause names the Lab's own account and is the owner's way in. Keep it:
    whether the first clause alone admits the owner depends on the chain, so relying on it
    would ship a file whose readability by its own owner varies by deployment.

    The evaluator walks the array flat and left to right. There is no nesting.
    """
    if not lab_account:
        raise ToolError(
            "The Lab's account address is required to build access conditions. Without the "
            "owner clause a file can end up readable by this agent and nobody else — including "
            "the human who owns the Lab."
        )
    config = endpoints()
    return [
        _has_role_condition(config["conditionChain"], config["accessResolver"], ocl_id, role),
        {"operator": "or"},
        _tba_owner_condition(config["conditionChain"], config["accessResolver"], lab_account),
    ]


def lab_account_from_ocl_id(ocl_id: str) -> str:
    """The Lab's own account is the low 20 bytes of the OCL id — the same slice the API uses
    for its labAccountAddress, so the two cannot disagree."""
    if not (ocl_id.startswith("0x") and len(ocl_id) == 66):
        raise ToolError(f"An OCL id is 0x followed by 64 hex characters; got {ocl_id!r}.")
    int(ocl_id, 16)
    return "0x" + ocl_id[-40:]


# ==========================================================================
# the API
# ==========================================================================


def _headers() -> dict[str, str]:
    """Authorization alone, plus a service token for authenticated writes.

    - No "Bearer" prefix: anything starting with it is routed to the user-session verifier,
      which tries to decode the credential as a JWT and denies it.
    - No API-key header alongside: the API's default auth mode is a shared key, so a request
      carrying both is resolved under that key and the credential is never seen. It may
      still succeed, which is exactly the problem.
    - No wallet-address header: it is not consulted on the service-token path.
    """
    credential = env("MOLECULE_CONSUMER_CREDENTIAL")
    if not credential:
        raise ToolError(
            "MOLECULE_CONSUMER_CREDENTIAL is not set. It is the mol_… string from the Molecule "
            "team — think of it as the API key for the Labs API. Save it with save_credential, "
            "or put it in .env and reconnect this server (/mcp)."
        )
    if not credential.startswith("mol_"):
        raise ToolError(
            "MOLECULE_CONSUMER_CREDENTIAL must be the mol_… string from the Molecule team, sent "
            "verbatim — never with a Bearer prefix, and never alongside an API-key header."
        )
    headers = {"Content-Type": "application/json", "Authorization": credential}
    token = env("MOLECULE_SERVICE_TOKEN")
    if token:
        # Read as exactly this or "X-Service-Token"; any other casing is ignored.
        headers["x-service-token"] = token
    return headers


def graphql(document: str, variables: dict | None = None) -> dict:
    response = _http.post(
        setting("MOLECULE_LABS_URL"),
        headers=_headers(),
        content=json.dumps({"query": document, "variables": variables or {}}),
    )
    try:
        body = response.json()
    except ValueError:
        raise ToolError(
            f"The Labs API returned a non-JSON response ({response.status_code}): "
            f"{response.text[:300]}"
        ) from None
    if body.get("errors"):
        # Reads fail here. A wrong or expired credential arrives as UnauthorizedException.
        raise ToolError(f"Labs API error: {json.dumps(body['errors'])[:600]}")
    if body.get("data") is None:
        raise ToolError(f"Labs API HTTP {response.status_code}: {response.text[:300]}")
    return body["data"]


def parse_details(details: Any) -> dict:
    """`details` is an object on thrown read errors, a JSON string in-band, and currently a
    doubly-encoded JSON string in-band. Parse until it stops being a string."""
    value = details
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            break
    return value if isinstance(value, dict) else {}


def assert_ok(result: dict | None, operation: str) -> dict:
    """Success is `error == null`. There is no `isSuccess` field on these types any more —
    selecting one is a validation error that fails the whole request."""
    if result is None:
        raise ToolError(f"{operation} returned no result.")
    error = result.get("error")
    if error:
        reason = parse_details(error.get("details")).get("reason")
        raise ToolError(
            f"{operation} failed: {error.get('code')}"
            + (f"/{reason}" if reason else "")
            + f": {error.get('message')}"
            + (f" (requestId {error.get('requestId')})" if error.get("requestId") else "")
        )
    return result


def _files_of(lab: dict | None) -> list[dict]:
    """The data room and its file list are both nullable."""
    if not lab:
        return []
    return ((lab.get("dataRoom") or {}).get("files")) or []


def _headers_map(pairs: Any) -> dict:
    return {h["key"]: h["value"] for h in (pairs or [])}


def normalise_path(path: str) -> str:
    """Stored paths carry a leading slash. Normalise both sides and compare exactly — a
    suffix match would let /2024-findings.csv satisfy a check for findings.csv."""
    return path if path.startswith("/") else "/" + path


# ---- documents -----------------------------------------------------------

Q_CATEGORIES = "query FileCategoriesAndTags { fileCategoriesAndTags { data { name tags } } }"

Q_MEMBERS = """query ListLabMembers($oclId: String!) {
  listLabMembers(oclId: $oclId) { members { walletAddress role source isAgent expiry grantedAt } }
}"""

Q_SIGN_IN = """query GetServiceSignInMessage($walletAddress: String!, $serviceName: String!) {
  getServiceSignInMessage(walletAddress: $walletAddress, serviceName: $serviceName) { message expiresAt }
}"""

M_TOKEN = """mutation GenerateServiceToken($serviceName: String!, $walletAddress: String!, $messageSignature: String!, $expiresIn: String) {
  generateServiceToken(serviceName: $serviceName, walletAddress: $walletAddress, messageSignature: $messageSignature, expiresIn: $expiresIn) {
    token tokenId expiresAt
    error { code message requestId retryable details }
  }
}"""

Q_LAB_BY_NAME = """query LabByShortname($shortname: String!) {
  labWithDataRoomAndFiles(shortname: $shortname) { oclId name shortname labAccountAddress }
}"""

Q_LAB = """query Lab($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    oclId shortname name labAccountAddress
    dataRoom { files { did id path contentType accessLevel version createdBy } }
  }
}"""

Q_DATA_ROOM = """query DataRoom($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    shortname
    dataRoom {
      files {
        did id path contentType contentHash version createdBy accessLevel
        downloadUrl downloadHeaders { key value } downloadUrlExpiry
        encryptionMetadata { encryptionSystem encryptedDek iv contentHash accessControlConditions }
      }
    }
  }
}"""

M_DEK = """mutation GenerateDataEncryptionKey {
  generateDataEncryptionKey {
    plaintextDEK encryptedDek encryptionSystem
    error { code message requestId retryable details }
  }
}"""

M_INITIATE = """mutation Initiate($oclId: String!, $contentType: String!, $contentLength: Int!) {
  initiateCreateOrUpdateFile(oclId: $oclId, contentType: $contentType, contentLength: $contentLength) {
    uploadToken uploadUrl uploadUrlExpiry method headers { key value }
    error { code message requestId retryable details }
  }
}"""

M_FINISH = """mutation Finish($oclId: String!, $uploadToken: String!, $path: String, $ref: String, $accessLevel: String!, $changeBy: String!, $description: String, $tags: [String!], $categories: [String!], $contentText: String, $encryptionMetadata: EncryptionMetadataInput) {
  finishCreateOrUpdateFile(oclId: $oclId, uploadToken: $uploadToken, path: $path, ref: $ref, accessLevel: $accessLevel, changeBy: $changeBy, description: $description, tags: $tags, categories: $categories, contentText: $contentText, encryptionMetadata: $encryptionMetadata) {
    datasetId contentHash version
    error { code message requestId retryable details }
  }
}"""

M_DECRYPT = """mutation DecryptDataKey($oclId: String!, $filePath: String!) {
  decryptDataKey(oclId: $oclId, filePath: $filePath) {
    plaintextDEK iv message
    error { code message requestId retryable details }
  }
}"""


# ==========================================================================
# wallet
# ==========================================================================


def _account():
    from eth_account import Account

    key = env("MOLECULE_AGENT_PRIVATE_KEY")
    if not key:
        raise ToolError(
            "MOLECULE_AGENT_PRIVATE_KEY is not set. Call agent_wallet(create=True, "
            "envFile='.env') once to create this agent's identity and store it."
        )
    body = key[2:] if key.startswith("0x") else key
    if len(body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        # eth-account left-pads odd-length hex, so a key one character short is accepted
        # and silently derives a DIFFERENT valid wallet — one with no role on the Lab.
        raise ToolError(
            f"MOLECULE_AGENT_PRIVATE_KEY must be 64 hex characters (optionally 0x-prefixed); "
            f"got {len(body)}. A key one character short is still a valid key — it just "
            f"belongs to a different wallet, with no role on any Lab."
        )
    return Account.from_key("0x" + body)


# ==========================================================================
# TOOLS — setup
# ==========================================================================


@mcp.tool()
def config_doctor() -> str:
    """Report what is configured, what is missing, and how to fix it. CALL THIS FIRST and
    show the human the result.

    The order matters: the API credential has to be in place before the wallet, because a
    wallet created first cannot look anything up, and the next step then fails for a reason
    that has nothing to do with the wallet.

    Never returns a secret — only whether each one is present, and where it came from."""
    issues: list[str] = []
    fixes: list[str] = []

    credential = env("MOLECULE_CONSUMER_CREDENTIAL")
    if not credential:
        issues.append("MOLECULE_CONSUMER_CREDENTIAL is not set — no API credential.")
        fixes.append(
            "Ask the human for their Molecule API credential: the mol_… string from their "
            "starter pack, or from the Molecule team if they do not have one. It works like an "
            "API key — it says whose quota a call belongs to and can be revoked — and it is "
            "NOT a wallet key. Save it with save_credential(credential='mol_…')."
        )
    elif not credential.startswith("mol_"):
        issues.append("MOLECULE_CONSUMER_CREDENTIAL does not look like a mol_… credential.")

    wallet = None
    if env("MOLECULE_AGENT_PRIVATE_KEY"):
        try:
            wallet = _account().address
        except ToolError as exc:
            issues.append(str(exc))
    elif not issues:
        issues.append("MOLECULE_AGENT_PRIVATE_KEY is not set — this agent has no identity yet.")
        fixes.append("Call agent_wallet(create=True, envFile='.env').")
    else:
        fixes.append(
            "Once the credential is saved, call agent_wallet(create=True, envFile='.env') — in "
            "that order."
        )

    try:
        config = endpoints()
    except ToolError as exc:
        config = None
        issues.append(str(exc))

    return dump(
        {
            "endpoints": config,
            "apiCredential": "set" if credential else "MISSING",
            "agentWallet": wallet or "not created yet",
            "serviceToken": "set" if env("MOLECULE_SERVICE_TOKEN") else "not issued yet",
            "configLoadedFrom": _ENV_SOURCES,
            "settingsBase": _CONFIG_BASE,
            "issues": issues,
            "fixes": fixes,
            "notes": [
                "This server reads configuration once, at spawn. After editing .env, reconnect "
                "it (/mcp) or the change will look like it had no effect.",
                "On macOS a .env is hidden in Finder — Cmd+Shift+. toggles hidden files.",
            ],
            "next_step": (
                "Show the human `issues` and `fixes`, and work through them in that order — "
                "credential first, wallet second."
                if issues
                else "Setup is complete. Show the human the wallet address, then ask which Lab "
                "they want to write to and pass the URL or name to resolve_lab."
            ),
        }
    )


@mcp.tool()
def save_credential(credential: str, envFile: str = ".env") -> str:
    """Store the human's Molecule API credential so this and later sessions can use it.

    `credential` is the mol_… string from their starter pack. It is written to envFile at
    mode 0600 and takes effect immediately. The value is never echoed back.

    Think of it as an API key for the Labs API: it identifies whose calls these are and can
    be revoked. It is NOT the agent's wallet key and NOT the human's wallet — it grants no
    signing power and holds no funds."""
    value = (credential or "").strip()
    if not value.startswith("mol_"):
        raise ToolError(
            "That does not look like a Molecule API credential — they start with 'mol_' and "
            "come from the starter pack or the Molecule team. Ask the human for theirs rather "
            "than inventing one."
        )
    action = upsert_env_file(envFile, "MOLECULE_CONSUMER_CREDENTIAL", value)
    return dump(
        {
            "stored": True,
            "envFile": str(Path(envFile).expanduser()),
            "action": f"{action} MOLECULE_CONSUMER_CREDENTIAL",
            "next_step": (
                "Do not repeat the credential back to the human or put it anywhere they might "
                "share. Now call agent_wallet(create=True, envFile=…) to create this agent's "
                "identity."
            ),
        }
    )


@mcp.tool()
def agent_wallet(create: bool = False, envFile: str | None = None) -> str:
    """Report this agent's wallet address, or create its identity.

    Reachable with no upload details at all: getting an address to hand the human must never
    require having decided what to upload.

    With create=True and envFile, generates a key, writes it into that .env as
    MOLECULE_AGENT_PRIVATE_KEY (creating the file at mode 0600, replacing any existing line)
    and returns ONLY the address. The key is never returned or logged.

    Call this AFTER the API credential is saved."""
    if not create:
        return dump(
            {
                "address": _account().address,
                "next_step": "Pass the Lab URL or name the human gives you to resolve_lab.",
            }
        )
    if not envFile:
        raise ToolError(
            "Pass envFile to say where the key should be stored — '.env' in the project is the "
            "usual answer. Ask the human first: generating a key with nowhere to put it wastes "
            "the Lab owner's role grant, because the next run would be a different agent."
        )
    from eth_account import Account

    account = Account.create()
    key_hex = account.key.hex()
    if not key_hex.startswith("0x"):  # eth-account >=0.13 returns bare hex
        key_hex = "0x" + key_hex
    action = upsert_env_file(envFile, "MOLECULE_AGENT_PRIVATE_KEY", key_hex)
    return dump(
        {
            "address": account.address,
            "envFile": str(Path(envFile).expanduser()),
            "action": f"{action} MOLECULE_AGENT_PRIVATE_KEY",
            "permissions": "0600" if os.name != "nt" else "not enforced on Windows",
            "next_step": (
                f"Tell the human, in these words: this agent's address is {account.address}. "
                f"Ask them to open their Lab, go to Members, and add that address with the "
                f"CONTRIBUTOR role — a Viewer can read but never upload. Then STOP and wait "
                f"until they say it is done. The key is saved in {envFile}; tell them to keep "
                f"it, because a new key is a different agent and they would have to grant the "
                f"role all over again."
            ),
        }
    )


@mcp.tool()
def envelope_self_test() -> str:
    """Verify this server's encryption matches the data-room format, before it is trusted
    with a confidential file. No network, no credentials, microseconds.

    Generates a random key in memory and checks the properties that decide whether the Labs
    app can open what this writes: a 12-byte IV, the auth tag appended rather than held
    separately, no additional authenticated data, and the content hash taken over the
    plaintext. Nothing is stored, and no fixed key material exists anywhere in this file."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    plaintext = secrets.token_bytes(64)
    result = encrypt_bytes(plaintext, key)
    checks = {
        "iv is 12 bytes": len(base64.b64decode(result["iv"])) == IV_BYTES,
        "auth tag is appended (ciphertext is plaintext + 16)": result["cipherBytes"]
        == len(plaintext) + TAG_BYTES,
        "content hash is the SHA-256 of the plaintext": result["contentHash"]
        == hashlib.sha256(plaintext).hexdigest(),
        "round trip returns the original bytes": decrypt_bytes(
            result["ciphertext"], result["iv"], key
        )
        == plaintext,
        "a fresh iv per call": encrypt_bytes(plaintext, key)["iv"] != result["iv"],
    }
    tampered = bytearray(result["ciphertext"])
    tampered[-1] ^= 0x01
    try:
        decrypt_bytes(bytes(tampered), result["iv"], key)
        checks["a tampered tag is rejected"] = False
    except Exception:
        checks["a tampered tag is rejected"] = True

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise ToolError(f"Envelope self-test FAILED: {failed}. Do not upload a private file.")
    return dump({"checks": checks, "ok": True})


# ==========================================================================
# TOOLS — finding the Lab
# ==========================================================================


@mcp.tool()
def resolve_lab(labUrlOrName: str) -> str:
    """Turn a Lab URL or its short name into the OCL id every other tool needs.

    Ask the human to paste the address bar from their Lab page. Anything of the shape
    …/labs/<name> works, and so does the bare <name> — the last path segment is the Lab's
    short name, which the API looks up directly, so nobody has to hunt for a 32-byte id. An
    OCL id itself is accepted too, if they happen to have one.

    Returns the OCL id, the display name, and the Lab's account address."""
    raw = (labUrlOrName or "").strip()
    if not raw:
        raise ToolError("Pass the Lab URL or its short name — whatever the human gave you.")

    if raw.startswith("0x") and len(raw) == 66:
        lab = graphql(Q_LAB, {"oclId": raw}).get("labWithDataRoomAndFiles")
        if not lab:
            raise ToolError(
                "No Lab with that OCL id here. Check the id, and check the API credential "
                "belongs to the same deployment as the Lab."
            )
        found_by = "the OCL id you gave"
    else:
        shortname = raw.rstrip("/").split("?")[0].split("#")[0].rsplit("/", 1)[-1].strip()
        if not shortname:
            raise ToolError(f"Could not read a Lab name out of {labUrlOrName!r}.")
        lab = graphql(Q_LAB_BY_NAME, {"shortname": shortname}).get("labWithDataRoomAndFiles")
        if not lab:
            raise ToolError(
                f"No Lab named {shortname!r} here. Ask the human to paste the full URL from "
                f"their Lab page, and check their API credential belongs to the same deployment "
                f"the Lab lives on."
            )
        found_by = f"the name {shortname!r} taken from the URL"

    return dump(
        {
            "oclId": lab["oclId"],
            "name": lab.get("name"),
            "shortname": lab.get("shortname"),
            "labAccountAddress": lab["labAccountAddress"],
            "foundBy": found_by,
            "next_step": (
                f"Tell the human you resolved {lab.get('name')!r}, so a wrong paste is caught "
                f"now rather than after an upload. Then call lab_members to see whether this "
                f"agent has a role yet."
            ),
        }
    )


@mcp.tool()
def lab_members(oclId: str) -> str:
    """List who holds a role on the Lab, and whether this agent is among them.

    A public read — the API credential alone, no service token. Addresses come back
    lowercased, and expiry is unix seconds as a decimal string, or null for permanent.

    Role grants reach the API through an event indexer, so one made seconds ago may not be
    visible yet. If this agent is absent, report that and wait for the human — do not spin."""
    members = (graphql(Q_MEMBERS, {"oclId": oclId}).get("listLabMembers") or {}).get(
        "members"
    ) or []
    try:
        address = _account().address
    except ToolError:
        address = None
    mine = next(
        (m for m in members if address and m["walletAddress"].lower() == address.lower()), None
    )
    role = (mine or {}).get("role")
    if role == "VIEWER":
        step = (
            "This agent holds VIEWER, which can read but never upload. Ask the human to change "
            "it to CONTRIBUTOR, and do not retry until they say they have."
        )
    elif role in ("CONTRIBUTOR", "OWNER"):
        step = "The grant is visible. Call issue_service_token."
    else:
        step = (
            f"This agent ({address}) has no role on this Lab yet. Ask the human to add it as "
            f"CONTRIBUTOR in the Members panel, then stop and wait. If they say they already "
            f"did, the role indexer may still be catching up — wait a little and call again."
        )
    return dump(
        {"agentAddress": address, "agentRole": role, "members": members, "next_step": step}
    )


@mcp.tool()
def issue_service_token(serviceName: str, expiresIn: str = "30d", envFile: str | None = None) -> str:
    """Self-issue the token that authenticates this agent's writes. Returns its id and expiry;
    the token itself stays in this process and is sent automatically.

    Call this only AFTER the role grant is visible. The sign-in message carries a single-use
    nonce that expires ten minutes after it is issued, so fetching it while waiting for the
    human wastes it. The nonce is one per (wallet, serviceName) and a new one overwrites the
    last, so two sign-ins under the same name at once clobber each other.

    `serviceName` is how the human tells this agent's token from any other when they come to
    revoke it — pick something task-specific and tell them what you chose.

    `expiresIn` is <int><unit> between 1 hour and 2 years. Use only s, m, h, d, w: the "M"
    unit is read as MINUTES by the token signer while the API reports it back as months, so
    "6M" claims six months and dies in six minutes.

    Pass envFile to save it too, so a later session need not issue a new one."""
    if expiresIn and expiresIn[-1] in ("M", "y"):
        raise ToolError(
            f'expiresIn={expiresIn!r} uses an unsafe unit. "M" is read as MINUTES by the token '
            f'signer while the API reports it back as months, so the token would die long '
            f'before it claims to. Use s, m, h, d or w — for example "30d".'
        )
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = _account()
    message = (
        graphql(Q_SIGN_IN, {"walletAddress": account.address, "serviceName": serviceName}).get(
            "getServiceSignInMessage"
        )
        or {}
    ).get("message")
    if not message:
        raise ToolError("The sign-in message came back empty.")
    # Sign the returned message verbatim — the server recomposes it byte-identically and any
    # difference fails verification. The wallet must be a plain EOA: verification recovers
    # the signer and has no contract-account fallback.
    signature = Account.sign_message(encode_defunct(text=message), account.key).signature.hex()
    if not signature.startswith("0x"):  # eth-account >=0.13 returns bare hex
        signature = "0x" + signature
    result = assert_ok(
        graphql(
            M_TOKEN,
            {
                "serviceName": serviceName,
                "walletAddress": account.address,
                "messageSignature": signature,
                "expiresIn": expiresIn,
            },
        ).get("generateServiceToken"),
        "generateServiceToken",
    )
    os.environ["MOLECULE_SERVICE_TOKEN"] = result["token"]
    saved = (
        f"{upsert_env_file(envFile, 'MOLECULE_SERVICE_TOKEN', result['token'])} "
        f"MOLECULE_SERVICE_TOKEN"
        if envFile
        else None
    )
    return dump(
        {
            "tokenId": result.get("tokenId"),
            "serviceName": serviceName,
            "expiresAt": result.get("expiresAt"),
            "boundTo": account.address,
            "savedTo": str(Path(envFile).expanduser()) if envFile else None,
            "action": saved,
            "next_step": (
                f"The token is held in this server and sent automatically. Tell the human its "
                f"name ({serviceName!r}) and expiry so they can revoke it later. Next call "
                f"lab_info to see what is already in the data room."
            ),
        }
    )


@mcp.tool()
def lab_info(oclId: str) -> str:
    """Read the Lab and the paths already used in its data room. Public read.

    This is the preflight, and it turns two permanent failures into questions: an unknown
    OCL id means the Lab is not on this deployment, and a path already in `existingPaths`
    can never be written again.

    Also returns the Lab's account address, which a private upload needs for its access
    conditions, cross-checked against the value the OCL id itself implies."""
    lab = graphql(Q_LAB, {"oclId": oclId}).get("labWithDataRoomAndFiles")
    if not lab:
        raise ToolError(
            "No Lab with that OCL id here. Check it character for character, and check the API "
            "credential belongs to the same deployment as the Lab."
        )
    derived = lab_account_from_ocl_id(oclId)
    if lab["labAccountAddress"].lower() != derived.lower():
        raise ToolError(
            f"The Lab's account address does not match its OCL id: the API says "
            f"{lab['labAccountAddress']}, the id implies {derived}. Stop and report this."
        )
    files = _files_of(lab)
    return dump(
        {
            "oclId": lab["oclId"],
            "name": lab.get("name"),
            "shortname": lab.get("shortname"),
            "labAccountAddress": lab["labAccountAddress"],
            "existingPaths": [f["path"] for f in files],
            "files": [
                {k: f.get(k) for k in ("path", "did", "contentType", "accessLevel", "version")}
                for f in files
            ],
            "next_step": (
                "Show the human the Lab name and the paths already in use, so they can choose a "
                "free one. Then call file_categories_and_tags."
            ),
        }
    )


@mcp.tool()
def file_categories_and_tags() -> str:
    """The category and tag vocabulary the API will accept, from its own content service.

    Take values from here rather than inventing them or copying a list from elsewhere — it
    drifts. One category, at most three tags, each tag belonging to that category, matched
    exactly. Validation runs before the file is committed, so a rejected finalize can be
    retried with corrected values; but it fails open when the content service is
    unreachable, so a successful upload is not proof the tags were right."""
    categories = (graphql(Q_CATEGORIES).get("fileCategoriesAndTags") or {}).get("data") or []
    return dump(
        {
            "categories": categories,
            "rules": [
                "exactly one category",
                "at most three tags",
                "each tag must belong to the category you send",
                "matched exactly — categories are lowercase, tags are Title-Case",
            ],
            "next_step": (
                "Offer the human the categories and their tags and let them choose. Then call "
                "stage_upload with every answer they have given you."
            ),
        }
    )


@mcp.tool()
def check_onchain_access(oclId: str, role: str = "contributor") -> str:
    """Read the access resolver's hasRole for this agent, directly on chain. No key, no spend.

    Required before a private upload, because this is the exact call the API makes when
    somebody tries to decrypt. It answers two questions at once:

    - a REVERT means the conditions about to be written could never be evaluated. The
      fail-closed evaluator would deny the whole array and the file would be permanently
      unopenable.
    - a clean FALSE — usually an expired grant — means those conditions would deny this
      agent, so it could not read back its own file.

    Either way: stop, and do not fall back to a public upload."""
    role_int = _ROLE_BY_NAME.get(role)
    if role_int is None:
        raise ToolError(f'role must be "viewer" or "contributor", got {role!r}.')
    config = endpoints()
    address = _account().address
    # hasRole(bytes32,address,uint8) — three fixed-width words, so no ABI encoder is needed.
    calldata = "0x1ece65ce" + oclId[2:] + address[2:].lower().rjust(64, "0") + f"{role_int:064x}"
    response = _http.post(
        config["rpcUrl"],
        headers={"content-type": "application/json"},
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": config["accessResolver"], "data": calldata}, "latest"],
            }
        ),
    )
    if response.status_code >= 400:
        raise ToolError(
            f"The RPC endpoint returned HTTP {response.status_code} — a transport or rate-limit "
            f"failure, not a contract error. Retry, or set EVM_RPC_URL to a dedicated endpoint. "
            f"Body: {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if body.get("error"):
        raise ToolError(
            f"hasRole reverted: {json.dumps(body['error'])[:300]}. The access conditions this "
            f"upload would write are unevaluatable, so the file could never be decrypted. Check "
            f"the OCL id and the chain configuration, and do not fall back to a public upload."
        )
    result = body.get("result")
    if not result or result == "0x":
        raise ToolError(
            f"hasRole returned no data — there is probably no access resolver at "
            f"{config['accessResolver']} on {config['chainName']}. Check the chain configuration."
        )
    if int(result, 16) != 1:
        raise ToolError(
            f"hasRole is FALSE for {address} on {config['chainName']}. The conditions this "
            f"upload would write would deny this agent, so it could not read back its own file. "
            f"The role grant may have expired. Do not fall back to a public upload — report "
            f"this to the human."
        )
    return dump(
        {
            "allowed": True,
            "address": address,
            "role": role,
            "chain": f"{config['chainName']} ({config['chainId']})",
            "next_step": "The lock is evaluatable and admits this agent. Continue with the plan.",
        }
    )


# ==========================================================================
# TOOLS — the confirmed plan
# ==========================================================================


def _require_confirmed_plan(plan_id: str) -> dict:
    if not _PLAN:
        raise ToolError(
            "No upload has been staged. Call stage_upload with the human's answers, read the "
            "plan back to them, and record their approval with confirm_upload_plan."
        )
    if plan_id != _PLAN["planId"]:
        raise ToolError(
            f"That plan id is not the staged plan ({_PLAN['planId']}). Re-stage and re-confirm."
        )
    if not _PLAN.get("confirmed"):
        raise ToolError(
            "The staged plan has not been confirmed by the human. Read them the plan from "
            "stage_upload — the visibility and the destination above all — and pass their answer "
            "to confirm_upload_plan. Nothing is written until then."
        )
    return _PLAN


@mcp.tool()
def stage_upload(
    oclId: str,
    filePath: str,
    visibility: Literal["public", "private"],
    description: str,
    category: str,
    tags: list[str],
    path: str | None = None,
    ref: str | None = None,
    contentType: str | None = None,
    contentText: str | None = None,
    conditionRole: Literal["viewer", "contributor"] = "contributor",
) -> str:
    """Record exactly what is about to happen and hand it back for the human to approve.

    Nothing is uploaded here. This validates every answer, resolves the content type, checks
    the destination is free, and returns a plain-language plan plus the questions to put to
    the human. Pass their reply to confirm_upload_plan.

    Every argument is an answer the human gives, never one to infer:

      visibility   "public" means plaintext that anyone with the link can download, for
                   good. "private" means encrypted, openable only by wallets the Lab's
                   access conditions admit. There is NO default and no safe guess. Ask.
      filePath     a file that already exists. If you are generating the contents, write
                   them out first, show the human what they say, and get an explicit
                   go-ahead — never generate and upload in one motion.
      path         where it lands in the data room (a NEW file), or `ref` for a new version
                   of an existing one. A taken path fails permanently, and the local
                   filename is a guess, not an answer.
      description  the one line they will read beside the file.

    The access level is a label, not a lock: this server encrypts nothing on its own, and a
    private-looking label on plaintext stores plaintext. Only the private path encrypts."""
    global _PLAN

    source = Path(filePath).expanduser()
    if not source.is_file():
        raise ToolError(f"{filePath} is not a file. It has to exist before it can be uploaded.")
    plaintext_size = source.stat().st_size
    if plaintext_size == 0:
        raise ToolError(f"{filePath} is empty — there is nothing to upload.")

    if not path and not ref:
        raise ToolError(
            f"Pass path= for a new file, or ref= for a new version of an existing one. Ask the "
            f"human which they want: a taken path fails permanently, and the local filename "
            f"({source.name!r}) is a guess, not an answer."
        )
    if path and ref:
        raise ToolError("Pass path= or ref=, never both.")
    if path and path.lstrip("/").startswith("agreements/"):
        raise ToolError("agreements/ is reserved by the API and cannot be written to.")

    resolved_type = contentType or MIME.get(source.suffix.lower())
    if not resolved_type:
        raise ToolError(
            f"Unrecognised extension {source.suffix!r}. Pass contentType= with the real type — "
            f"it is stored and shown to the human, and guessing would put a mislabelled file in "
            f"their data room."
        )
    if not (description or "").strip():
        raise ToolError(
            "description is required. It is the line the human reads beside the file; without "
            "it the upload is indistinguishable from junk in someone else's data room."
        )
    tags = list(tags or [])
    if not tags:
        raise ToolError("At least one tag is required, belonging to the category you send.")
    if len(tags) > 3:
        raise ToolError(f"At most three tags; got {len(tags)}.")

    # Validate the vocabulary now, not after the bytes are already uploaded.
    catalogue = (graphql(Q_CATEGORIES).get("fileCategoriesAndTags") or {}).get("data") or []
    known = next((c for c in catalogue if c["name"] == category), None)
    if not known:
        raise ToolError(
            f"Unknown category {category!r}. Valid: {', '.join(c['name'] for c in catalogue)}"
        )
    for tag in tags:
        if tag not in known["tags"]:
            raise ToolError(
                f"Tag {tag!r} does not belong to category {category!r}. "
                f"Valid: {', '.join(known['tags'])}"
            )

    lab = graphql(Q_LAB, {"oclId": oclId}).get("labWithDataRoomAndFiles")
    if not lab:
        raise ToolError("No Lab with that OCL id here. Resolve it again before going on.")
    files = _files_of(lab)
    wanted = normalise_path(path) if path else None
    if wanted and wanted in [f["path"] for f in files]:
        raise ToolError(
            f"{wanted!r} already exists in this data room. Uploading to a taken path fails "
            f"permanently and cannot be retried. Ask the human for a different path, or use "
            f"ref= to add a new version of the existing file."
        )
    if ref and not any(ref in (f.get("did"), f.get("id")) for f in files):
        raise ToolError(
            f"ref={ref!r} is not a file in this data room. Confirm it with the human, or pass "
            f"path= for a new file."
        )

    confidential = visibility == "private"
    warnings: list[str] = []
    if contentText and confidential:
        warnings.append(
            "The searchable text is stored UNENCRYPTED beside the ciphertext and is readable by "
            "anyone who can query the data room. Confirm the human meant that exact text to be "
            "public, or drop it."
        )
    if confidential and conditionRole == "contributor":
        warnings.append(
            "Openable by Contributors and the Lab owner, but NOT by read-only Viewers. The Labs "
            "app locks its own confidential files at Viewer level, so this is one notch tighter "
            "than a file the human uploads through the app themselves. Use "
            "conditionRole='viewer' if they want everyone they have invited to be able to open "
            "it."
        )

    _PLAN = {
        "planId": f"plan_{uuid.uuid4().hex[:12]}",
        "oclId": oclId,
        "labName": lab.get("name"),
        "shortname": lab.get("shortname"),
        "labAccountAddress": lab["labAccountAddress"],
        "filePath": str(source.resolve()),
        "plaintextBytes": plaintext_size,
        "visibility": visibility,
        "accessLevel": "ADMIN" if confidential else "PUBLIC",
        "conditionRole": conditionRole if confidential else None,
        "path": wanted,
        "ref": ref,
        "contentType": resolved_type,
        "description": description,
        "category": category,
        "tags": tags,
        "contentText": contentText,
        "confirmed": False,
        "confirmation": None,
        "published": False,
    }

    destination = wanted or f"a new version of {ref}"
    summary = (
        f"Upload {source.name} ({plaintext_size} bytes, {resolved_type}) into "
        f"{lab.get('name') or 'the Lab'}, at {destination}, as "
        + (
            f"PRIVATE — encrypted, openable by {conditionRole}-and-above on this Lab"
            if confidential
            else "PUBLIC — plaintext, downloadable by anyone with the link, permanently"
        )
        + f'. Description: "{description}". Category {category}, tags {tags}.'
    )
    return dump(
        {
            "planId": _PLAN["planId"],
            "summary": summary,
            "plan": {
                k: v
                for k, v in _PLAN.items()
                if k not in ("confirmed", "confirmation", "published")
            },
            "warnings": warnings,
            "ask_the_human": [
                f"Visibility: {visibility.upper()}"
                + (
                    " — anyone with the link will be able to download this file, permanently. "
                    "Is that right?"
                    if not confidential
                    else " — encrypted. Is that right?"
                ),
                f"Destination: {destination} in {lab.get('name') or oclId}. Correct?",
                f'Description they will see: "{description}". Correct?',
            ]
            + warnings,
            "next_step": (
                "Show the human `summary`, every question in `ask_the_human`, and every "
                "`warning`, then WAIT for their answer. If they approve, pass their own words to "
                "confirm_upload_plan. If they change anything, call stage_upload again with the "
                "correction — never confirm a plan they did not agree to."
            ),
        }
    )


@mcp.tool()
def confirm_upload_plan(planId: str, humanApproval: str) -> str:
    """Record the human's approval of a staged plan. Required before anything is written.

    `humanApproval` is what they actually said, quoted — not your summary of it, and not a
    placeholder. If they have not answered yet, do not call this: stop and ask them.

    Passing an approval the human did not give defeats the only safeguard between a
    confidential document and a public URL."""
    if not _PLAN:
        raise ToolError("No plan is staged. Call stage_upload first.")
    if planId != _PLAN["planId"]:
        raise ToolError(
            f"That plan id is not the staged plan ({_PLAN['planId']}) — it changed after being "
            f"shown to the human. Re-stage it and show them the new one."
        )
    text = (humanApproval or "").strip()
    if len(text) < 2 or text.lower() in {
        "n/a", "none", "tbd", "assumed", "implied", "pending", "ok?", "-",
    }:
        raise ToolError(
            f"{text!r} is not an approval from the human. Ask them directly, and quote what "
            f"they say."
        )
    _PLAN["confirmed"] = True
    _PLAN["confirmation"] = text
    return dump(
        {
            "planId": planId,
            "confirmed": True,
            "recorded": text,
            "visibility": _PLAN["visibility"],
            "next_step": (
                "Now run it: upload_public_file for a public file, upload_private_file for a "
                "private one. Both refuse unless this confirmation is in place."
            ),
        }
    )


# ==========================================================================
# TOOLS — the upload
# ==========================================================================


def _initiate(ocl_id: str, content_type: str, content_length: int) -> dict:
    return assert_ok(
        graphql(
            M_INITIATE,
            {"oclId": ocl_id, "contentType": content_type, "contentLength": content_length},
        ).get("initiateCreateOrUpdateFile"),
        "initiateCreateOrUpdateFile",
    )


def _put(upload: dict, data: bytes) -> None:
    response = _http.request(
        (upload.get("method") or "PUT").upper(),
        upload["uploadUrl"],
        headers=_headers_map(upload.get("headers")),
        content=data,
    )
    if response.status_code >= 400:
        raise ToolError(
            f"The upload to storage failed ({response.status_code}): {response.text[:300]}. If "
            f"the presigned URL expired — see uploadUrlExpiry — run the initiate step again."
        )


def _finish(plan: dict, upload_token: str, metadata: dict | None) -> dict:
    assert_confidential_finalize_ok(plan["oclId"], plan["accessLevel"], bool(metadata))
    account = _account()
    return assert_ok(
        graphql(
            M_FINISH,
            {
                "oclId": plan["oclId"],
                "uploadToken": upload_token,
                "path": plan["path"],
                "ref": plan["ref"],
                "accessLevel": plan["accessLevel"],
                # The Labs app sends did:ethr:<address>, and its file viewer only renders
                # attribution when this parses as that. A bare address uploads fine and
                # then shows no author at all.
                "changeBy": f"did:ethr:{account.address}",
                "description": plan["description"],
                "tags": plan["tags"],
                "categories": [plan["category"]],
                "contentText": plan["contentText"],
                "encryptionMetadata": metadata,
            },
        ).get("finishCreateOrUpdateFile"),
        "finishCreateOrUpdateFile",
    )


@mcp.tool()
def upload_public_file(planId: str) -> str:
    """Upload the confirmed plan's file as PLAINTEXT, downloadable by anyone, permanently.

    Refuses unless the human confirmed the plan, and refuses if the plan says private. Once
    this returns, the file is published and cannot be un-published."""
    plan = _require_confirmed_plan(planId)
    if plan["visibility"] != "public":
        raise ToolError(
            "This plan is PRIVATE. Call upload_private_file. Never publish in the clear a file "
            "the human asked to keep confidential."
        )
    data = Path(plan["filePath"]).read_bytes()
    assert_not_confidential_plaintext(plan["filePath"], data)
    upload = _initiate(plan["oclId"], plan["contentType"], len(data))
    _put(upload, data)
    finished = _finish(plan, upload["uploadToken"], None)
    plan["published"] = True
    return dump(
        {
            "published": True,
            "datasetId": finished.get("datasetId"),
            "version": finished.get("version"),
            "path": plan["path"] or plan["ref"],
            "accessLevel": plan["accessLevel"],
            "next_step": (
                "The file IS published. Call verify_upload, then give the human the link and say "
                "plainly that it went up as PUBLIC."
            ),
        }
    )


@mcp.tool()
def upload_private_file(planId: str) -> str:
    """Encrypt the confirmed plan's file and upload the ciphertext.

    Runs the whole confidential sequence in one step, deliberately: a data key is issued and
    kept in this process, the file is encrypted locally, the access conditions are built and
    checked against this Lab, the ciphertext is uploaded, and the file is finalized with
    encryption metadata and a non-public access level. Keeping it together is what makes the
    guarantee hold — there is no window in which anything can interleave a plaintext upload.

    Refuses unless the human confirmed the plan, and refuses if the plan says public. If any
    part fails the run aborts: do NOT retry it as a public upload."""
    plan = _require_confirmed_plan(planId)
    if plan["visibility"] != "private":
        raise ToolError("This plan is PUBLIC. Call upload_public_file.")

    envelope_self_test()  # prove the format before trusting it with a confidential file
    check_onchain_access(plan["oclId"], plan["conditionRole"])

    conditions = lab_conditions(
        plan["oclId"], plan["labAccountAddress"], _ROLE_BY_NAME[plan["conditionRole"]]
    )
    # Nothing on the server checks that the conditions name THIS Lab, so check here.
    if conditions[0]["functionParams"][0].lower() != plan["oclId"].lower():
        raise ToolError("The access conditions do not name this OCL id. Aborting.")
    if conditions[0]["functionParams"][1] != ":userAddress":
        raise ToolError("The access conditions must keep the literal :userAddress placeholder.")
    if conditions[2]["functionParams"][1].lower() != plan["labAccountAddress"].lower():
        raise ToolError("The access conditions do not name this Lab's account. Aborting.")
    mark_confidential_lab(plan["oclId"])

    dek = assert_ok(graphql(M_DEK).get("generateDataEncryptionKey"), "generateDataEncryptionKey")
    handle = put_dek(dek["plaintextDEK"])  # the plaintext key never enters a tool result
    plaintext = Path(plan["filePath"]).read_bytes()
    encrypted = encrypt_bytes(plaintext, get_dek(handle))
    mark_confidential(plan["filePath"], encrypted["contentHash"])
    del plaintext

    # The declared length must be the CIPHERTEXT length; the plaintext length produces a
    # presigned URL the body does not match and an unhelpful 403.
    upload = _initiate(plan["oclId"], plan["contentType"], encrypted["cipherBytes"])
    _put(upload, encrypted["ciphertext"])

    metadata = {
        # Omitting this does not mean "no system" — it routes the payload to a legacy
        # validator that demands a different field set and rejects it.
        "encryptionSystem": dek.get("encryptionSystem") or "kms",
        "accessControlConditions": json.dumps(conditions, separators=(",", ":")),
        "encryptedBy": _account().address,
        "encryptedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "encryptedDek": dek["encryptedDek"],  # already base64 — do not re-encode
        "iv": encrypted["iv"],
        "contentHash": encrypted["contentHash"],  # SHA-256 of the PLAINTEXT
    }
    finished = _finish(plan, upload["uploadToken"], metadata)
    plan["uploadedContentHash"] = encrypted["contentHash"]
    plan["published"] = True
    return dump(
        {
            "published": True,
            "datasetId": finished.get("datasetId"),
            "version": finished.get("version"),
            "path": plan["path"] or plan["ref"],
            "accessLevel": plan["accessLevel"],
            "plaintextBytes": plan["plaintextBytes"],
            "ciphertextBytes": encrypted["cipherBytes"],
            "openableBy": f"{plan['conditionRole']}-and-above on this Lab, and the Lab owner",
            "next_step": (
                "The file IS published, as ciphertext. Call verify_upload — a successful finalize "
                "proves nothing about the encryption, because the API never checks the metadata "
                "against the bytes. Then tell the human it went up as PRIVATE, openable by "
                f"{plan['conditionRole']}-and-above, and ask them to confirm they can open it in "
                "the app."
            ),
        }
    )


@mcp.tool()
def verify_upload(planId: str) -> str:
    """Read the committed file back, and for a private upload download and open it.

    A successful finalize proves nothing about the encryption: the API validates the
    metadata's shape but never checks it against the bytes, and its own test fixtures upload
    random values in those fields. The only real proof is decrypting what was stored.

    Any failure here is a failure of the CHECK, not of the upload — the file is already
    published. Say that plainly rather than implying nothing happened."""
    plan = _require_confirmed_plan(planId)
    if not plan.get("published"):
        raise ToolError("This plan has not been uploaded yet — there is nothing to verify.")
    config = endpoints()

    # A just-finalized file can take a moment to appear in the data-room listing —
    # measured on a real upload, so poll rather than concluding it is missing. Anything
    # that fails here has still been published, which is why the message says so.
    stored = None
    lab = None
    for attempt in range(6):
        lab = graphql(Q_DATA_ROOM, {"oclId": plan["oclId"]}).get("labWithDataRoomAndFiles")
        files = _files_of(lab)
        if plan["path"]:
            stored = next((f for f in files if f["path"] == plan["path"]), None)
        else:
            stored = next((f for f in files if plan["ref"] in (f.get("did"), f.get("id"))), None)
        if stored:
            break
        if attempt < 5:
            time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s, 8s, 10s
    if not stored:
        raise ToolError(
            "The file IS published, but it has not appeared in the data-room listing yet. Do "
            "NOT re-upload it — the path is taken and a second attempt would fail permanently. "
            "Wait a moment and call verify_upload again."
        )

    result: dict[str, Any] = {
        "path": stored["path"],
        "accessLevel": stored["accessLevel"],
        "version": stored.get("version"),
        "createdBy": stored.get("createdBy"),
        "encrypted": bool(stored.get("encryptionMetadata")),
    }
    if plan["visibility"] == "private":
        key = assert_ok(
            graphql(M_DECRYPT, {"oclId": plan["oclId"], "filePath": stored["path"]}).get(
                "decryptDataKey"
            ),
            "decryptDataKey",
        )
        blob = _http.get(
            stored["downloadUrl"], headers=_headers_map(stored.get("downloadHeaders"))
        ).content
        iv = key.get("iv") or (stored.get("encryptionMetadata") or {}).get("iv")
        opened = decrypt_bytes(blob, iv, key["plaintextDEK"])
        if hashlib.sha256(opened).hexdigest() != plan.get("uploadedContentHash"):
            raise ToolError(
                "Round trip FAILED: the stored bytes are not the file that was encrypted. The "
                "file IS published — tell the human before doing anything else."
            )
        result["roundTrip"] = "downloaded the stored file, decrypted it, plaintext hash matches"

    shortname = (lab or {}).get("shortname")
    return dump(
        {
            **result,
            "link": f"{config['labsApp']}/labs/{shortname}" if shortname else None,
            "next_step": (
                f"Tell the human the file is up, name the visibility explicitly "
                f"({plan['visibility'].upper()}), and give them the link."
                + (
                    " Ask them to confirm they can open it in the app — that is the only check "
                    "that proves the owner, not just this agent, can read it."
                    if plan["visibility"] == "private"
                    else ""
                )
            ),
        }
    )


# ==========================================================================
# TOOLS — reading
# ==========================================================================


@mcp.tool()
def list_data_room_files(oclId: str) -> str:
    """List the data room's files, their access levels, and whether each is encrypted.

    A public read — a Contributor can read as well as write. Note that the access level does
    not gate the download: the ciphertext of a private file is fetchable by anyone who can
    run this query. Confidentiality rests on the encryption, not on the label, so never tell
    a human a private file "cannot be downloaded" — it can, as ciphertext."""
    lab = graphql(Q_DATA_ROOM, {"oclId": oclId}).get("labWithDataRoomAndFiles")
    return dump(
        {
            "shortname": (lab or {}).get("shortname"),
            "files": [
                {
                    "path": f["path"],
                    "did": f.get("did"),
                    "contentType": f.get("contentType"),
                    "accessLevel": f.get("accessLevel"),
                    "version": f.get("version"),
                    "createdBy": f.get("createdBy"),
                    "encrypted": bool(f.get("encryptionMetadata")),
                }
                for f in _files_of(lab)
            ],
            "note": (
                "Two different fields are called a content hash: a file's own is the data room's "
                "digest of the bytes it stored — the ciphertext, for an encrypted file — while "
                "the encryption metadata's is the SHA-256 of the plaintext. They can never be "
                "equal for an encrypted file."
            ),
        }
    )


@mcp.tool()
def read_data_room_file(oclId: str, path: str, outPath: str) -> str:
    """Download one file, decrypting it if it is encrypted, and write it to outPath.

    Decryption needs this agent to pass two gates: its wallet must hold at least Viewer on
    the Lab (a database check that lags the chain) and the file's stored conditions must
    admit it (a live chain read). Both denials arrive as UNAUTHORIZED and differ only in the
    reason — "UNAUTHORIZED" is the index catching up, so wait and retry, while
    "ACCESS_DENIED" means the conditions genuinely exclude this wallet and retrying will
    never help."""
    lab = graphql(Q_DATA_ROOM, {"oclId": oclId}).get("labWithDataRoomAndFiles")
    wanted = normalise_path(path)
    stored = next((f for f in _files_of(lab) if f["path"] == wanted), None)
    if not stored:
        raise ToolError(f"{wanted} is not in this data room.")
    blob = _http.get(
        stored["downloadUrl"], headers=_headers_map(stored.get("downloadHeaders"))
    ).content
    metadata = stored.get("encryptionMetadata")
    if metadata:
        system = (metadata.get("encryptionSystem") or "").lower()
        if system in ("", "lit"):
            raise ToolError(
                "This file predates the current encryption format and cannot be opened through "
                "this flow. Report it to the human; there is no workaround."
            )
        key = assert_ok(
            graphql(M_DECRYPT, {"oclId": oclId, "filePath": stored["path"]}).get("decryptDataKey"),
            "decryptDataKey",
        )
        blob = decrypt_bytes(blob, key.get("iv") or metadata["iv"], key["plaintextDEK"])
    target = Path(outPath).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    return dump(
        {
            "path": stored["path"],
            "writtenTo": str(target),
            "bytes": len(blob),
            "wasEncrypted": bool(metadata),
        }
    )


# ==========================================================================
# boot
# ==========================================================================


def main() -> None:
    log("mol-labs-mcp ready (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
