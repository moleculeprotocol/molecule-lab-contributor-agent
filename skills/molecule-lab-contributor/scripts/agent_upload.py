#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "eth-account>=0.13.7",
#   "cryptography>=42",
#   "httpx>=0.27",
# ]
#
# [tool.uv]
# # Never compile a dependency from source. eth-account pulls C/Rust extensions
# # (ckzg, bitarray, cytoolz, pydantic-core) and cryptography has a Rust core; the
# # newest release of some of those ships arm64-only macOS wheels, so without this an
# # Intel Mac silently drops to a source build that needs a compiler and a Rust
# # toolchain — an ~80s wait at best, a wall of build errors at worst. With it, uv
# # resolves to the newest version that HAS a wheel for the machine it is on
# # (e.g. cryptography 48.x on Intel Mac, 50.x on Apple Silicon), and if no wheel
# # exists anywhere it says so plainly instead of trying to build.
# no-build = true
# ///
"""Upload one file into a Molecule Lab data room as a Contributor.

This script has no defaults for the choices that cannot be undone. It will not guess a
visibility, a data-room path, a description or an environment — if you have not been told,
go and ask. Every one of those refusals is deliberate: a file published to the wrong place,
or published in the clear when it should have been encrypted, cannot be recalled.

    uv run --env-file .env agent_upload.py \\
      --file ./findings.csv \\
      --visibility <the human's answer — never yours> \\
      --path findings.csv \\
      --description "Round 3 assay results" \\
      --category science --tag Discovery

Run with --help for the full surface, or --dry-run to rehearse everything up to the first
write. See SKILL.md for what each answer means.

Secrets are read from the environment, never from flags: argv is visible in `ps` and lands
verbatim in an agent's transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import access_conditions as ac  # noqa: E402
    import kms_envelope  # noqa: E402
    from labs_api import (  # noqa: E402
        ENVIRONMENTS,
        ApiError,
        LabsClient,
        LabsError,
        assert_ok,
        has_role_on_chain,
        redact,
        with_indexer_lag_retry,
    )
except ImportError as exc:  # pragma: no cover - a setup problem, not a runtime one
    # Reached when this file is run with a bare `python3` instead of through uv, so the
    # dependency block at the top was never resolved. Name the fix instead of dumping a
    # traceback about a package the reader never asked for.
    sys.stderr.write(
        f"Missing dependency: {exc.name}\n\n"
        "Run this through uv, which reads the dependency block at the top of this file and\n"
        "installs everything (including a suitable Python) on first use:\n\n"
        "    uv run agent_upload.py --help\n\n"
        "If uv is not installed:\n"
        "    curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux\n"
        "    brew install uv                                     # or, with Homebrew\n"
        "    powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"   # Windows\n"
    )
    raise SystemExit(1)

EXIT_OK, EXIT_ERROR, EXIT_WAITING, EXIT_REFUSED = 0, 1, 2, 3

# The stored contentType is what the human sees in the data room. There is deliberately no
# mimetypes.guess_type fallback: it returns plausible-but-wrong types for unknown
# extensions, and this flow would rather stop and ask than ship a mislabelled file.
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


class GateRefusal(Exception):
    """An input the human has to supply is missing or unusable. Exit code 3."""


class WaitingForGrant(Exception):
    """The agent's wallet has no role on the Lab yet. Exit code 2."""


@dataclass(frozen=True)
class UploadResult:
    dry_run: bool
    dataset_id: str
    version: int | None
    path: str
    access_level: str
    visibility: str
    condition_role: str | None
    agent_address: str
    app_url: str | None
    content_hash: str | None
    verified: bool

    def __str__(self) -> str:
        what = "encrypted" if self.visibility == "private" else "public"
        who = f", readable by {self.condition_role}-and-above" if self.condition_role else ""
        if self.dry_run:
            return (
                f"DRY RUN — nothing was uploaded. Would have written {self.path} as {what} "
                f"({self.access_level}){who}."
            )
        return (
            f"Uploaded {self.path} as {what} ({self.access_level}){who}. "
            f"datasetId {self.dataset_id}"
            + (f" — {self.app_url}" if self.app_url else "")
        )


# --------------------------------------------------------------------------
# Phase 1 — identity. Deliberately reachable without any upload answers, so getting a
# wallet address to hand the human never requires pretending to have decided a visibility.
# --------------------------------------------------------------------------


def generate_agent_key(out_path: str | os.PathLike) -> str:
    """Create the agent's wallet, write the key at 0600, and return only the address.

    Refuses to overwrite. The target is often a .env that already holds the consumer
    credential, and a second run would otherwise discard an identity the human has already
    granted a role to — wasting their grant and silently becoming a different agent.
    """
    from eth_account import Account

    path = Path(out_path)
    account = Account.create()
    key_hex = account.key.hex()
    if not key_hex.startswith("0x"):  # eth-account >=0.13 returns bare hex
        key_hex = "0x" + key_hex
    try:
        # O_EXCL, not open("w"): "w" truncates, and the mode argument is umask-masked.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise GateRefusal(
            f"{path} already exists. It may hold an agent key or your environment, and "
            f"overwriting it would discard an identity the Lab owner has already granted a "
            f"role to. Choose a different path, or set AGENT_PRIVATE_KEY from the existing file."
        ) from None
    with os.fdopen(fd, "w") as handle:
        handle.write(f"AGENT_PRIVATE_KEY={key_hex}\n")
    os.chmod(path, 0o600)  # the open() mode is masked by umask; this is not
    if os.name == "nt":
        print(f"warning: file permissions are not enforced on Windows — protect {path}", file=sys.stderr)
    return account.address


def _account_from_env(agent_private_key: str):
    """Load the agent's wallet, refusing a malformed key.

    eth-account left-pads an odd-length hex string, so a key missing one character is
    accepted and silently derives a *different, valid* wallet — one the Lab owner has not
    granted anything to. The address is printed twice before the grant is requested, but
    catching it here costs nothing. Never put the key itself in the message.
    """
    from eth_account import Account

    body = agent_private_key[2:] if agent_private_key.startswith("0x") else agent_private_key
    if len(body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise GateRefusal(
            f"AGENT_PRIVATE_KEY must be 64 hex characters (optionally 0x-prefixed); got "
            f"{len(body)} characters. A key one character short is still a valid key — it "
            f"just belongs to a different wallet, with no role on the Lab."
        )
    return Account.from_key("0x" + body)


# --------------------------------------------------------------------------
# The gate. Everything here runs before a single byte reaches the network.
# --------------------------------------------------------------------------


def _require(value, what: str, ask: str):
    if value is None or value == "" or value == []:
        raise GateRefusal(f"Missing {what}. {ask}")
    return value


def _validate_inputs(
    *,
    env: str | None,
    ocl_id: str | None,
    visibility: str | None,
    file: str | os.PathLike | None,
    path: str | None,
    ref: str | None,
    description: str | None,
    category: str | None,
    tags: Sequence[str],
    content_type: str | None,
    condition_role: str,
    expires_in: str,
) -> dict:
    if env not in ENVIRONMENTS:
        raise GateRefusal(
            'Set the environment to "staging" or "production" (MOLECULE_ENV or --env). There '
            "is no default. Ask the human which app they created their Lab in: "
            "testnet.labs.molecule.xyz is staging, labs.molecule.xyz is production. A Lab in "
            "one is invisible in the other."
        )

    _require(ocl_id, "the Lab's oclId (OCL_ID or --ocl-id)", "Ask the human to copy it from the Lab in the app.")
    if not (ocl_id.startswith("0x") and len(ocl_id) == 66):
        raise GateRefusal(f"oclId must be 0x + 64 hex characters, got {ocl_id!r}")

    _require(
        visibility,
        "--visibility",
        'Ask the human: "public" (anyone can download the file, permanently) or "private" '
        "(encrypted; only people with a role on this Lab can open it). Do not choose for them.",
    )
    if visibility not in ("public", "private"):
        raise GateRefusal(f'--visibility must be "public" or "private", got {visibility!r}')

    _require(
        file,
        "--file",
        "The file must already exist on disk. If you are generating the content, write it "
        "out first, show the human what it says, and get an explicit go-ahead before uploading.",
    )
    source = Path(file)
    if not source.is_file():
        raise GateRefusal(f"{source} is not a file")
    plaintext = source.read_bytes()
    if not plaintext:
        raise GateRefusal(f"{source} is empty — nothing to upload")

    if not path and not ref:
        raise GateRefusal(
            "Pass --path <name> for a new file, or --ref <datasetId> for a new version of an "
            "existing one. Confirm which with the human: a path that is already taken fails "
            f"permanently, and the local filename ({source.name!r}) is a guess, not an answer."
        )
    if path and ref:
        raise GateRefusal("Pass --path or --ref, never both")
    if path and path.lstrip("/").startswith("agreements/"):
        raise GateRefusal("agreements/ is reserved by the backend and cannot be written to")

    _require(
        description,
        "--description",
        "One line the human will read next to this file in the app. An untitled, undescribed "
        "file is indistinguishable from junk in someone else's data room.",
    )

    resolved_type = content_type or MIME.get(source.suffix.lower())
    if not resolved_type:
        raise GateRefusal(
            f"Unrecognised extension {source.suffix!r}. Pass --content-type with the real MIME "
            f"type — this value is stored and shown to the human."
        )

    _require(category, "--category", "Exactly one, from the fileCategoriesAndTags query.")
    tags = list(tags or [])
    if not tags:
        raise GateRefusal("Pass at least one --tag belonging to --category")
    if len(tags) > 3:
        raise GateRefusal(f"At most 3 tags; got {len(tags)}")

    if condition_role not in ("viewer", "contributor"):
        raise GateRefusal('--condition-role must be "viewer" or "contributor"')

    # Checked before any network call: "M" is read as MINUTES by the token signer while the
    # API reports it back to you as months, so a "6M" token dies in six minutes.
    if expires_in and expires_in[-1] in ("M", "y"):
        raise GateRefusal(
            f"EXPIRES_IN={expires_in!r} uses a unit that is unsafe here. \"M\" is read as MINUTES "
            f"by the token signer while the API reports it back as months, so the token dies long "
            f"before it says it will. Use s, m, h, d or w — e.g. \"30d\"."
        )

    return {"plaintext": plaintext, "source": source, "content_type": resolved_type, "tags": tags}


# --------------------------------------------------------------------------
# GraphQL documents
# --------------------------------------------------------------------------

Q_CATEGORIES = "query FileCategoriesAndTags { fileCategoriesAndTags { data { name tags } } }"

Q_MEMBERS = """query ListLabMembers($oclId: String!) {
  listLabMembers(oclId: $oclId) { members { walletAddress role isAgent expiry } }
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

Q_LAB = """query Lab($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    shortname labAccountAddress
    dataRoom { files { did id path } }
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

Q_VERIFY = """query Verify($oclId: String!) {
  labWithDataRoomAndFiles(oclId: $oclId) {
    shortname
    dataRoom {
      files {
        did id path accessLevel version createdBy
        downloadUrl downloadHeaders { key value } downloadUrlExpiry
        encryptionMetadata { encryptionSystem iv }
      }
    }
  }
}"""

M_DECRYPT = """mutation DecryptDataKey($oclId: String!, $filePath: String!) {
  decryptDataKey(oclId: $oclId, filePath: $filePath) {
    plaintextDEK iv message
    error { code message requestId retryable details }
  }
}"""


def _files_of(lab: dict | None) -> list[dict]:
    """dataRoom and files are both nullable in the schema."""
    if not lab:
        return []
    return ((lab.get("dataRoom") or {}).get("files")) or []


def _normalise_path(path: str) -> str:
    """Stored paths carry a leading slash. Normalise both sides and compare exactly —
    a suffix match would let /2024-findings.csv satisfy a check for findings.csv."""
    return path if path.startswith("/") else "/" + path


def _headers_map(pairs) -> dict:
    return {h["key"]: h["value"] for h in (pairs or [])}


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------


def upload_file(
    *,
    env: str,
    ocl_id: str,
    consumer_credential: str,
    agent_private_key: str,
    visibility: str,
    file: str | os.PathLike,
    description: str,
    category: str,
    tags: Sequence[str],
    path: str | None = None,
    ref: str | None = None,
    content_type: str | None = None,
    content_text: str | None = None,
    condition_role: str = "contributor",
    service_name: str | None = None,
    expires_in: str = "30d",
    rpc_url: str | None = None,
    dry_run: bool = False,
    wait_for_grant: float = 300.0,
    on_progress: Callable[[str], None] | None = None,
) -> UploadResult:
    """Run the whole flow. Keyword-only, and `visibility`, `description`, `category` and
    `tags` have no defaults — so a notebook caller who omits the visibility gets a
    TypeError rather than a published file. `file` is a path, never bytes: "write it out,
    show the human, get a go-ahead" must not be short-circuitable.
    """
    say = on_progress or (lambda message: None)

    checked = _validate_inputs(
        env=env, ocl_id=ocl_id, visibility=visibility, file=file, path=path, ref=ref,
        description=description, category=category, tags=tags, content_type=content_type,
        condition_role=condition_role, expires_in=expires_in,
    )
    plaintext = checked["plaintext"]
    resolved_type = checked["content_type"]
    tags = checked["tags"]
    confidential = visibility == "private"

    settings = ENVIRONMENTS[env]
    account = _account_from_env(agent_private_key)
    say(f"1/6 Agent wallet: {account.address}  (env: {env})")

    with LabsClient(graphql_url=settings["graphql_url"], consumer_credential=consumer_credential) as api:
        # Validate the category and tags against the API's own list before anything moves.
        # The server checks these too, but only at the last call — after the bytes are in
        # S3 — and it fails open when its CMS is unreachable.
        catalogue = (api.query(Q_CATEGORIES).get("fileCategoriesAndTags") or {}).get("data") or []
        known = next((c for c in catalogue if c["name"] == category), None)
        if not known:
            raise GateRefusal(
                f"Unknown category {category!r}. Valid: {', '.join(c['name'] for c in catalogue)}"
            )
        for tag in tags:
            if tag not in known["tags"]:
                raise GateRefusal(
                    f"Tag {tag!r} does not belong to category {category!r}. "
                    f"Valid: {', '.join(known['tags'])}"
                )

        # ---- Phase 2/3: wait for the owner's grant (public query, no service token) ----
        grant = None
        deadline = time.monotonic() + max(wait_for_grant, 0)
        asked = False
        while True:
            members = api.query(Q_MEMBERS, {"oclId": ocl_id})["listLabMembers"]["members"]
            grant = next(
                (m for m in members if m["walletAddress"].lower() == account.address.lower()), None
            )
            if grant or time.monotonic() >= deadline:
                break
            if not asked:
                say(f"2/6 Waiting. Ask the Lab owner to add {account.address} as Contributor.")
                asked = True
            time.sleep(5)
        if not grant:
            raise WaitingForGrant(
                f"No role grant found for {account.address} on this Lab. Ask the owner to add it "
                f"as a Contributor, and confirm you are pointed at the right environment ({env})."
            )
        if grant["role"] == "VIEWER":
            raise GateRefusal(
                "This wallet holds VIEWER. Uploading needs CONTRIBUTOR — ask the owner to change it."
            )
        say(f"3/6 Role: {grant['role']}, expiry: {grant.get('expiry') or 'permanent'}")

        # ---- Phase 4: self-issue a service token ----
        # Only now: the sign-in message carries a single-use nonce that dies 10 minutes
        # after issue, so fetching it before the human has acted wastes it.
        from eth_account import Account
        from eth_account.messages import encode_defunct

        name = service_name or f"lab-contributor-{account.address[2:8]}"
        message = api.query(
            Q_SIGN_IN, {"walletAddress": account.address, "serviceName": name}
        )["getServiceSignInMessage"]["message"]
        # Sign the returned message verbatim — the server recomposes it byte-identically.
        signature = Account.sign_message(encode_defunct(text=message), agent_private_key).signature
        signature_hex = signature.hex()
        if not signature_hex.startswith("0x"):  # eth-account >=0.13 returns bare hex
            signature_hex = "0x" + signature_hex
        token = assert_ok(
            api.query(
                M_TOKEN,
                {
                    "serviceName": name,
                    "walletAddress": account.address,
                    "messageSignature": signature_hex,
                    "expiresIn": expires_in,
                },
            )["generateServiceToken"],
            "generateServiceToken",
        )
        api.service_token = token["token"]
        say(f"4/6 Token issued as {name!r}, expires {token['expiresAt']}")

        # ---- Phase 5 preflight ----
        lab = api.query(Q_LAB, {"oclId": ocl_id})["labWithDataRoomAndFiles"]
        if not lab:
            raise GateRefusal(
                f"This Lab is not registered on {env} — check the oclId and the environment."
            )
        shortname = lab.get("shortname")
        lab_account_address = lab["labAccountAddress"]
        existing = _files_of(lab)

        wanted = _normalise_path(path) if path else None
        if wanted and any(f["path"] == wanted for f in existing):
            raise GateRefusal(
                f"{wanted!r} already exists in this data room. Uploading to an occupied path "
                f"fails permanently and cannot be retried. Ask the human: a different path, or a "
                f"new version of the existing file (--ref <datasetId>)?"
            )
        if ref and not any(ref in (f.get("did"), f.get("id")) for f in existing):
            raise GateRefusal(
                f"--ref {ref} is not a file in this data room. Confirm the datasetId with the "
                f"human, or pass --path for a new file."
            )

        derived = ac.lab_account_address_from_ocl_id(ocl_id)
        if lab_account_address.lower() != derived.lower():
            raise LabsError(
                f"labAccountAddress mismatch: the API says {lab_account_address}, the oclId "
                f"implies {derived}."
            )

        encryption_metadata = None
        role_name = None
        content_hash = None

        if not confidential:
            access_level = "PUBLIC"
            upload_bytes = plaintext
            say(f"5/6 Public upload: {len(plaintext)} bytes as {resolved_type}")
        else:
            # ============================================================
            # From here there is no public fallback. If anything below fails the run
            # aborts and the file is NOT uploaded — do not "recover" by re-running as
            # --visibility public.
            # ============================================================
            access_level = "ADMIN"  # what the Labs app writes for a confidential file
            role_name = condition_role

            # Prove this build of the envelope still matches the Labs client before
            # encrypting anything real.
            kms_envelope.self_test()

            # Prove the lock will be evaluatable, and that it admits us. A revert means
            # the conditions could never be checked; a clean false means they would deny.
            if not has_role_on_chain(
                rpc_url=rpc_url or settings["rpc_url"],
                access_resolver=settings["access_resolver"],
                ocl_id=ocl_id,
                address=account.address,
                role=ac.role_from_name(condition_role),
            ):
                raise LabsError(
                    f"AccessResolver.hasRole(oclId, {account.address}, "
                    f"{ac.role_from_name(condition_role)}) is false on chain "
                    f"{settings['chain_id']} — this agent could not read its own file back. "
                    f"Check whether the grant has expired."
                )

            conditions = ac.create_lab_access_conditions(
                access_resolver_address=settings["access_resolver"],
                chain_id=settings["chain_id"],
                lab_account_address=lab_account_address,
                ocl_id=ocl_id,
                role=ac.role_from_name(condition_role),
            )
            ac.assert_conditions_target_lab(
                conditions, ocl_id=ocl_id, lab_account_address=lab_account_address
            )

            dek = assert_ok(api.query(M_DEK)["generateDataEncryptionKey"], "generateDataEncryptionKey")
            # The plaintext DEK lives only in this scope. Never logged, never written.
            encrypted = kms_envelope.encrypt(plaintext, dek["plaintextDEK"])
            upload_bytes = encrypted.ciphertext
            content_hash = encrypted.content_hash

            # The interlock: whatever we are about to send must not be the plaintext.
            if hashlib.sha256(upload_bytes).hexdigest() == encrypted.content_hash:
                raise LabsError("refusing to upload: the outgoing bytes are the plaintext")

            encryption_metadata = {
                # Omitting this does not mean "no system" — it routes the payload to a
                # legacy validator that demands a different field set and rejects it.
                "encryptionSystem": dek.get("encryptionSystem") or "kms",
                "accessControlConditions": ac.to_json(conditions),
                "encryptedBy": account.address,
                "encryptedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "encryptedDek": dek["encryptedDek"],  # already base64 — do not re-encode
                "iv": encrypted.iv,
                "contentHash": encrypted.content_hash,  # SHA-256 of the PLAINTEXT
            }
            say(
                f"5/6 Private upload: {len(plaintext)} plaintext bytes -> "
                f"{encrypted.cipher_bytes} ciphertext bytes, readable by "
                f"{condition_role}-and-above on this Lab"
            )

        if dry_run:
            # Be precise about what a rehearsal already did: a service token exists (the
            # human will see it in their token list and may want to revoke it), and on the
            # private path a data key was minted. Neither is a data-room write.
            minted = " a data key was minted," if confidential else ""
            say(
                f"    --dry-run: a service token was issued,{minted} and the encryption was "
                f"rehearsed. Stopping before any data-room write — nothing was uploaded."
            )
            return UploadResult(
                dry_run=True,
                dataset_id="", version=None, path=wanted or (ref or ""), access_level=access_level,
                visibility=visibility, condition_role=role_name, agent_address=account.address,
                app_url=f"{settings['lab_app_url']}/projects/{shortname}" if shortname else None,
                content_hash=content_hash, verified=False,
            )

        # ---- Phase 5: the three upload calls ----
        def _do_upload() -> dict:
            initiated = assert_ok(
                api.query(
                    M_INITIATE,
                    {
                        "oclId": ocl_id,
                        "contentType": resolved_type,
                        # For an encrypted file this is the CIPHERTEXT length. Declaring
                        # the plaintext length produces a presigned URL the body does not
                        # match, and a bare 403.
                        "contentLength": len(upload_bytes),
                    },
                )["initiateCreateOrUpdateFile"],
                "initiateCreateOrUpdateFile",
            )
            # This is the only place bytes leave the process, and `upload_bytes` is the
            # only thing it can send. On the private path that holds the ciphertext.
            api.put_bytes(
                initiated["uploadUrl"],
                upload_bytes,
                method=initiated.get("method") or "PUT",
                headers=_headers_map(initiated.get("headers")),
            )
            if confidential and (access_level == "PUBLIC" or not encryption_metadata):
                raise LabsError(
                    "refusing to finalize a confidential file as PUBLIC or without metadata"
                )
            return assert_ok(
                api.query(
                    M_FINISH,
                    {
                        "oclId": ocl_id,
                        "uploadToken": initiated["uploadToken"],
                        "path": wanted,
                        "ref": ref,
                        "accessLevel": access_level,
                        # The Labs app sends did:ethr:<address> and its file viewer only
                        # renders "By <address>" when createdBy parses as that. A bare
                        # address uploads fine and then shows no attribution at all.
                        "changeBy": f"did:ethr:{account.address}",
                        "description": description,
                        "tags": list(tags),
                        "categories": [category],
                        "contentText": content_text,
                        "encryptionMetadata": encryption_metadata,
                    },
                )["finishCreateOrUpdateFile"],
                "finishCreateOrUpdateFile",
            )

        finished = with_indexer_lag_retry(_do_upload, on_progress=say)
        say(f"    Uploaded — datasetId {finished['datasetId']}, version {finished.get('version')}")

        # THE FILE IS NOW COMMITTED. Everything below is a check on an already-published
        # file, so every failure from here on has to carry that fact out with it — the
        # caller's error handling cannot tell "the upload failed" from "the upload
        # succeeded and the check failed" by looking at an exception type. Telling a human
        # nothing was published while a confidential file sits in their data room is its
        # own kind of harm, and re-running would then hit the occupied-path refusal.
        try:
            return _verify_and_report(
                api=api, ocl_id=ocl_id, finished=finished, wanted=wanted,
                confidential=confidential, content_hash=content_hash, account=account,
                visibility=visibility, role_name=role_name, settings=settings,
                shortname=shortname, say=say,
            )
        except Exception as err:
            raise LabsError(
                f"datasetId {finished['datasetId']} IS PUBLISHED at "
                f"{wanted or ref} as {access_level} — do NOT re-upload it. The check that "
                f"runs afterwards then failed: {type(err).__name__}: {err}"
            ) from err


def _verify_and_report(
    *, api, ocl_id, finished, wanted, confidential, content_hash, account,
    visibility, role_name, settings, shortname, say,
) -> UploadResult:
    """Read the committed file back and, for a private upload, open it.

    Split out so that every failure in here is wrapped by the caller with the fact that
    the file is already published.
    """
    verify = api.query(Q_VERIFY, {"oclId": ocl_id})["labWithDataRoomAndFiles"]
    files = _files_of(verify)
    if wanted:
        stored = next((f for f in files if f["path"] == wanted), None)
    else:
        stored = next(
            (f for f in files if finished["datasetId"] in (f.get("did"), f.get("id"))), None
        )
    if not stored:
        raise LabsError(
            f"Uploaded (datasetId {finished['datasetId']}) but the file is not visible in the "
            f"data room yet. It IS published — do not re-upload."
        )

    verified = False
    if confidential:
        # A successful finish proves nothing about the crypto: the backend never
        # validates encryptionMetadata against the bytes. Download what was actually
        # stored and open it as ourselves.
        key = assert_ok(
            api.query(M_DECRYPT, {"oclId": ocl_id, "filePath": stored["path"]})["decryptDataKey"],
            "decryptDataKey",
        )
        downloaded = api.get_bytes(
            stored["downloadUrl"], headers=_headers_map(stored.get("downloadHeaders"))
        )
        opened = kms_envelope.decrypt(
            downloaded,
            key.get("iv") or (stored.get("encryptionMetadata") or {}).get("iv"),
            key["plaintextDEK"],
        )
        if hashlib.sha256(opened).hexdigest() != content_hash:
            raise LabsError(
                "round trip failed: the stored bytes are not the file we encrypted. The file "
                "IS published — tell the human before doing anything else."
            )
        verified = True
        say("6/6 Verified: downloaded the stored file, decrypted it, plaintext hash matches")
    else:
        verified = True
        say(f"6/6 Verified: {stored['path']} is {stored['accessLevel']}, version {stored['version']}")

    # Compare format-tolerantly: createdBy comes back as we sent it, but older files
    # and other clients use a bare address.
    stored_by = (stored.get("createdBy") or "").rsplit(":", 1)[-1]
    if stored_by.lower() != account.address.lower():
        say(f"    Note: createdBy is {stored.get('createdBy')}, not this agent")

    app_url = f"{settings['lab_app_url']}/projects/{shortname}" if shortname else None
    return UploadResult(
        dry_run=False,
        dataset_id=finished["datasetId"],
        version=finished.get("version"),
        path=stored["path"],
        access_level=stored["accessLevel"],
        visibility=visibility,
        condition_role=role_name,
        agent_address=account.address,
        app_url=app_url,
        content_hash=content_hash,
        verified=verified,
    )


def list_files(*, env: str, ocl_id: str, consumer_credential: str) -> list[dict]:
    """Read the data room. Public query — no service token, no role needed."""
    settings = ENVIRONMENTS[env]
    with LabsClient(graphql_url=settings["graphql_url"], consumer_credential=consumer_credential) as api:
        return _files_of(api.query(Q_VERIFY, {"oclId": ocl_id})["labWithDataRoomAndFiles"])


def download_file(
    *, env: str, ocl_id: str, consumer_credential: str, path: str, agent_private_key: str | None = None,
    service_token: str | None = None,
) -> bytes:
    """Fetch one file, decrypting it when it carries encryptionMetadata.

    Decrypting needs a service token bound to a wallet with a role on the Lab — pass one,
    or pass the agent key to issue one.
    """
    settings = ENVIRONMENTS[env]
    wanted = _normalise_path(path)
    with LabsClient(graphql_url=settings["graphql_url"], consumer_credential=consumer_credential) as api:
        files = _files_of(api.query(Q_VERIFY, {"oclId": ocl_id})["labWithDataRoomAndFiles"])
        stored = next((f for f in files if f["path"] == wanted), None)
        if not stored:
            raise LabsError(f"{wanted} is not in this data room")
        blob = api.get_bytes(stored["downloadUrl"], headers=_headers_map(stored.get("downloadHeaders")))
        meta = stored.get("encryptionMetadata")
        if not meta:
            return blob
        if service_token:
            api.service_token = service_token
        elif agent_private_key:
            api.service_token = _issue_token(api, agent_private_key)
        else:
            raise LabsError(
                f"{wanted} is encrypted. Pass agent_private_key or service_token to decrypt it."
            )
        key = assert_ok(
            api.query(M_DECRYPT, {"oclId": ocl_id, "filePath": stored["path"]})["decryptDataKey"],
            "decryptDataKey",
        )
        return kms_envelope.decrypt(blob, key.get("iv") or meta["iv"], key["plaintextDEK"])


def _issue_token(api: LabsClient, agent_private_key: str, service_name: str | None = None) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.from_key(agent_private_key)
    name = service_name or f"lab-contributor-{account.address[2:8]}"
    message = api.query(Q_SIGN_IN, {"walletAddress": account.address, "serviceName": name})[
        "getServiceSignInMessage"
    ]["message"]
    signature = Account.sign_message(encode_defunct(text=message), agent_private_key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    result = assert_ok(
        api.query(
            M_TOKEN,
            {
                "serviceName": name,
                "walletAddress": account.address,
                "messageSignature": signature,
                "expiresIn": "30d",
            },
        )["generateServiceToken"],
        "generateServiceToken",
    )
    return result["token"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_upload.py",
        description=(
            "Upload one file into a Molecule Lab data room as a Contributor. There are no "
            "defaults for the choices that cannot be undone — if you have not been told, ask."
        ),
        epilog=(
            "Environment (secrets are env-only, never flags — argv is visible in `ps`):\n"
            "  MOLECULE_ENV=staging|production   required, no default\n"
            "  CONSUMER_CREDENTIAL=mol_...       required\n"
            "  OCL_ID=0x...                      required\n"
            "  AGENT_PRIVATE_KEY=0x...           required after the first run\n"
            "  SERVICE_NAME=...                  optional; how the human identifies your token\n"
            "  EXPIRES_IN=30d                    optional; units s/m/h/d/w only, never M\n"
            "  EVM_RPC_URL=...                   optional; overrides the public Base RPC\n"
            "\nLoad a .env with: uv run --env-file .env agent_upload.py ...\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", help="the file to upload (must already exist on disk)")
    parser.add_argument("--visibility", choices=["public", "private"],
                        help="public = anyone can download it; private = encrypted. Ask the human.")
    parser.add_argument("--path", help="where it lands in the data room (a NEW file)")
    parser.add_argument("--ref", help="datasetId of an existing file, to add a NEW VERSION")
    parser.add_argument("--description", help="one line the human will read next to the file")
    parser.add_argument("--category", help="exactly one, from fileCategoriesAndTags")
    parser.add_argument("--tag", action="append", default=[], help="repeatable, at most 3")
    parser.add_argument("--content-type", help="required only if the extension is unrecognised")
    parser.add_argument("--content-text", help="searchable text, stored IN THE CLEAR even for a private file")
    parser.add_argument("--condition-role", choices=["viewer", "contributor"], default="contributor",
                        help="who can decrypt a private file (default: contributor, which excludes Viewers)")
    parser.add_argument("--key-out", help="write a freshly generated agent key here, then stop")
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), help="overrides MOLECULE_ENV")
    parser.add_argument("--ocl-id", help="overrides OCL_ID")
    parser.add_argument("--wait-for-grant", type=float, default=300.0,
                        help="seconds to wait for the owner's role grant; 0 = check once and exit 2")
    parser.add_argument("--dry-run", action="store_true",
                        help="rehearse everything up to the first write, then stop")
    parser.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    parser.add_argument("--debug", action="store_true", help="show tracebacks")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    say = lambda message: print(message, file=sys.stderr)  # noqa: E731  progress -> stderr

    try:
        # Identity first, and reachable with no upload answers at all: getting an address
        # to hand the human must never require pretending to have decided a visibility.
        if args.key_out or not os.environ.get("AGENT_PRIVATE_KEY"):
            if not args.key_out:
                raise GateRefusal(
                    "No AGENT_PRIVATE_KEY set. Ask the human where to store a new one, then "
                    "re-run with --key-out <path>. Generating a key with nowhere to put it "
                    "wastes the owner's role grant: the next run would be a different agent."
                )
            address = generate_agent_key(args.key_out)
            say(f"Generated a new agent wallet: {address}")
            say(f"Key written to {args.key_out} (mode 0600). Back it up — a new key is a new agent.")
            say(f"Next: ask the Lab owner to add {address} as a Contributor, then re-run.")
            if args.json:
                print(json.dumps({"agentAddress": address, "keyPath": str(args.key_out)}))
            else:
                print(address)
            return EXIT_OK

        result = upload_file(
            env=args.env or os.environ.get("MOLECULE_ENV"),
            ocl_id=args.ocl_id or os.environ.get("OCL_ID"),
            consumer_credential=os.environ.get("CONSUMER_CREDENTIAL", ""),
            agent_private_key=os.environ["AGENT_PRIVATE_KEY"],
            visibility=args.visibility,
            file=args.file,
            description=args.description,
            category=args.category,
            tags=args.tag,
            path=args.path,
            ref=args.ref,
            content_type=args.content_type,
            content_text=args.content_text,
            condition_role=args.condition_role,
            service_name=os.environ.get("SERVICE_NAME"),
            expires_in=os.environ.get("EXPIRES_IN", "30d"),
            rpc_url=os.environ.get("EVM_RPC_URL"),
            dry_run=args.dry_run,
            wait_for_grant=args.wait_for_grant,
            on_progress=say,
        )
        print(json.dumps(asdict(result), indent=2) if args.json else str(result))
        return EXIT_OK

    except GateRefusal as err:
        say(f"\n{redact(str(err))}")
        say("\nNothing was uploaded. Get the answer from the human and run again.")
        return EXIT_REFUSED
    except WaitingForGrant as err:
        say(f"\n{redact(str(err))}")
        return EXIT_WAITING
    except (LabsError, ApiError) as err:
        say(f"\n{redact(str(err))}")
        if args.visibility == "private":
            # Any failure after the commit carries "IS PUBLISHED" in its own message, so
            # the absence of that phrase is itself the signal — no pattern-matching needed.
            published = "IS PUBLISHED" in str(err)
            say(
                "\nThis was a confidential upload. The file IS in the data room — say so, and "
                "do not re-upload it."
                if published
                else "\nThis was a confidential upload and it did not complete. The file was NOT "
                "published."
            )
            say("Either way, do not re-run this as --visibility public to get past the error.")
        if args.debug:
            raise
        return EXIT_ERROR
    except Exception as err:  # noqa: BLE001
        if args.debug:
            raise
        say(f"\n{type(err).__name__}: {redact(str(err))}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
