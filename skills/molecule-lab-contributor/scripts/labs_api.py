"""Every socket this program can open lives in this file.

`kms_envelope.py` and `access_conditions.py` deliberately import nothing that can reach
the network, so a reviewer deciding whether to trust this with a confidential file only
has to read one module to see where bytes can go:

    grep -n '^import\\|^from' scripts/*.py

Holds the GraphQL client, the header rules, the error contract, the S3 transfers, the
read-only chain call, the indexer-lag retry, and secret redaction.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from eth_utils import function_signature_to_4byte_selector

HTTP_TIMEOUT = 120.0

# There is no default environment. A Lab created in one is invisible in the other, and
# guessing produces a NOT_FOUND that reads like a bad oclId.
ENVIRONMENTS: dict[str, dict[str, Any]] = {
    "staging": {
        "graphql_url": "https://staging.graphql.api.molecule.xyz/graphql",
        "lab_app_url": "https://testnet.labs.molecule.xyz",
        "chain_id": 84532,  # Base Sepolia
        "access_resolver": "0x5493F472602C87318EA5Eff753cDD593bf9bF559",
        "rpc_url": "https://sepolia.base.org",
    },
    "production": {
        "graphql_url": "https://production.graphql.api.molecule.xyz/graphql",
        "lab_app_url": "https://labs.molecule.xyz",
        "chain_id": 8453,  # Base
        "access_resolver": "0x89a14Be8f7824d4775053Edad0f2fA2d6767b72B",
        "rpc_url": "https://mainnet.base.org",
    },
}

# --------------------------------------------------------------------------
# Redaction. Secrets reach this program through the environment, but they end up in
# exception strings, and an agent's exception strings end up in a transcript.
# --------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"mol_[A-Za-z0-9_\-]+"),  # consumer credential
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])"),  # private key (also matches an oclId)
)


def redact(text: str) -> str:
    """Blank out anything that looks like a credential. Applied to every message printed.

    The private-key pattern also matches an oclId, which is public — losing an oclId from
    an error message is a fair price for never printing a key.
    """
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("<redacted>", out)
    return out


class LabsError(Exception):
    """An error safe to show the user. Message is redacted at construction."""

    def __init__(self, message: str):
        super().__init__(redact(message))


@dataclass
class ApiError(Exception):
    """An in-band mutation failure — the `error: ApiError` every Labs mutation returns.

    Branch on `code`, and on `reason` for the cases where one code covers two very
    different situations (decrypt's two gates both surface as UNAUTHORIZED).
    """

    operation: str
    code: str
    message: str
    request_id: str | None = None
    retryable: bool = False
    reason: str | None = None

    def __str__(self) -> str:
        bits = f"{self.operation} failed: {self.code}"
        if self.reason:
            bits += f"/{self.reason}"
        bits += f": {redact(self.message)}"
        if self.request_id:
            bits += f" (requestId {self.request_id})"
        return bits


def parse_details(details: Any) -> dict:
    """`details` is an object on thrown query errors, a JSON string in-band, and currently
    a doubly-encoded JSON string in-band. Parse until it stops being a string."""
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
    """Success is `error == null` — never a truthy payload field. (There is no `isSuccess`
    on these types any more; selecting one is a validation error that fails the request.)"""
    if result is None:
        raise LabsError(f"{operation} returned no result")
    error = result.get("error")
    if error:
        raise ApiError(
            operation=operation,
            code=error.get("code") or "UNKNOWN",
            message=error.get("message") or "",
            request_id=error.get("requestId"),
            retryable=bool(error.get("retryable")),
            reason=parse_details(error.get("details")).get("reason"),
        )
    return result


class LabsClient:
    """Talks to the Labs GraphQL API as a consumer credential, optionally as a service token."""

    def __init__(self, *, graphql_url: str, consumer_credential: str, timeout: float = HTTP_TIMEOUT):
        if not consumer_credential:
            raise LabsError("A mol_ consumer credential is required")
        if not consumer_credential.startswith("mol_"):
            raise LabsError(
                "The consumer credential must be a mol_<consumerId>_<secret> string, sent "
                "verbatim as Authorization."
            )
        self._url = graphql_url
        self._credential = consumer_credential
        self.service_token: str | None = None
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LabsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        # Authorization ALONE.
        #
        # - No "Bearer": the gateway routes anything starting with "Bearer " to the Privy
        #   JWT verifier, which tries to decode the credential as a token and denies it.
        # - No x-api-key: the API's default auth mode is the shared API key, so a request
        #   carrying both is resolved under the shared key and the consumer credential is
        #   never seen. It may still succeed, which is exactly the problem.
        # - No x-wallet-address: it is not consulted on the service-token path.
        headers = {"Content-Type": "application/json", "Authorization": self._credential}
        if self.service_token:
            # Read as exactly this or "X-Service-Token"; any other casing is ignored.
            headers["x-service-token"] = self.service_token
        return headers

    def query(self, document: str, variables: dict | None = None) -> dict:
        """Run an operation. Query failures arrive in top-level errors[] and raise here;
        mutation failures arrive in-band and are raised by assert_ok."""
        response = self._http.post(
            self._url,
            headers=self._headers(),
            content=json.dumps({"query": document, "variables": variables or {}}),
        )
        try:
            body = response.json()
        except ValueError:
            raise LabsError(
                f"Labs GraphQL returned non-JSON ({response.status_code}): {response.text[:300]}"
            ) from None
        if body.get("errors"):
            raise LabsError(f"GraphQL error: {json.dumps(body['errors'])[:600]}")
        if body.get("data") is None:
            raise LabsError(f"Labs GraphQL HTTP {response.status_code}: {response.text[:300]}")
        return body["data"]

    def put_bytes(self, url: str, data: bytes, *, method: str = "PUT", headers: dict) -> None:
        """Upload to a presigned URL with exactly the headers `initiate` returned.

        This goes to S3, not the API — no Authorization, no service token. httpx adds its
        own accept/user-agent/content-length, which is harmless because a presigned URL
        only covers the headers it signed.
        """
        response = self._http.request(method.upper() or "PUT", url, headers=headers, content=data)
        if response.status_code >= 400:
            raise LabsError(f"Upload failed ({response.status_code}): {response.text[:300]}")

    def get_bytes(self, url: str, *, headers: dict | None = None) -> bytes:
        response = self._http.get(url, headers=headers or {})
        if response.status_code >= 400:
            raise LabsError(f"Download failed ({response.status_code}): {response.text[:300]}")
        return response.content


# --------------------------------------------------------------------------
# Read-only chain call — the private path's preflight.
# --------------------------------------------------------------------------

# Computed rather than hardcoded so the selector cannot silently drift from the signature.
_HAS_ROLE_SELECTOR = function_signature_to_4byte_selector("hasRole(bytes32,address,uint8)")


def has_role_on_chain(
    *, rpc_url: str, access_resolver: str, ocl_id: str, address: str, role: int, timeout: float = 30.0
) -> bool:
    """Read AccessResolver.hasRole over a public RPC.

    This is the exact call the backend makes at decrypt time. If it reverts here — wrong
    oclId, wrong resolver, wrong chain — it will revert there too, and the fail-closed
    evaluator will deny the whole condition array without saying why. So a revert means
    the file we are about to write could never be opened, and the run must stop.

    Three fixed-width words, so no ABI encoder is needed.
    """
    calldata = "0x" + (
        _HAS_ROLE_SELECTOR
        + bytes.fromhex(ocl_id[2:])
        + bytes.fromhex(address[2:].lower().rjust(64, "0"))
        + role.to_bytes(32, "big")
    ).hex()
    with httpx.Client(timeout=timeout) as http:
        response = http.post(
            rpc_url,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": access_resolver, "data": calldata}, "latest"],
                }
            ),
        )
    if response.status_code >= 400:
        raise LabsError(
            f"RPC returned HTTP {response.status_code} — a transport or rate-limit failure, not "
            f"a contract error. Retry, or set EVM_RPC_URL to a dedicated endpoint. "
            f"Body: {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if body.get("error"):
        raise LabsError(
            f"AccessResolver.hasRole reverted: {json.dumps(body['error'])[:300]}. The access "
            f"conditions this run would write are unevaluatable, so the file could never be "
            f"decrypted. Check the environment, the oclId and the RPC URL."
        )
    result = body.get("result")
    if not result or result == "0x":
        raise LabsError(
            f"AccessResolver.hasRole returned no data — there is probably no contract at "
            f"{access_resolver} on this RPC. Check the environment and the RPC URL."
        )
    return int(result, 16) == 1


# --------------------------------------------------------------------------
# Indexer lag.
# --------------------------------------------------------------------------

# Role state reaches the API through an event indexer, so for a window after the owner's
# grant lands on-chain a write still returns UNAUTHORIZED. Re-issuing the token does not
# help and neither does re-granting. Wait.
_LAGGY_CODES = frozenset({"UNAUTHORIZED", "NOT_FOUND"})

# Only these reasons are the lag. Everything else sharing those codes — and the backend's
# reason vocabulary is large, e.g. every Kamu "…NotFound" typename is passed through
# verbatim — is permanent, and retrying it re-uploads the whole payload up to 12 times.
# An allowlist, because we can enumerate what IS transient far better than what is not.
_LAGGY_REASONS = frozenset({None, "UNAUTHORIZED", "PROJECT_NOT_FOUND"})


def with_indexer_lag_retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 12,
    base_ms: int = 2000,
    cap_ms: int = 30000,
    on_progress: Callable[[str], None] | None = None,
) -> Any:
    """Retry only the failures that genuinely clear on their own.

    Inspects the structured code and reason rather than pattern-matching a message, so a
    permanent failure that happens to share a code with the lag is not retried forever.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except ApiError as err:
            laggy = err.code in _LAGGY_CODES and err.reason in _LAGGY_REASONS
            if not laggy or attempt == attempts - 1:
                raise
            delay = min(base_ms * 2**attempt, cap_ms) / 1000
            if on_progress:
                on_progress(
                    f"    indexer not caught up ({err.code}, attempt {attempt + 1}/{attempts}); "
                    f"retrying in {delay:.0f}s"
                )
            time.sleep(delay)
