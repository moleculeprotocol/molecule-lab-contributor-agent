"""Access control conditions for an encrypted Molecule Labs data-room file.

These conditions are the lock. The backend holds the wrapped key and hands it to anyone
this array evaluates true for, so what you write here decides — permanently — who can
ever open the file.

Two properties make mistakes here expensive:

1. Nothing validates the array at upload time. Not that it refers to this Lab, not that
   the resolver address exists, not that the chain string is one the evaluator knows. A
   wrong value uploads perfectly and then fails every decrypt forever.
2. Evaluation is fail-closed and not fault-tolerant. Any error thrown while evaluating
   denies the WHOLE array, so a broken first clause takes the owner's clause down with
   it — the `or` never gets the chance to save you.

So: emit the shape the Labs app emits, and check it before sending.

This module imports nothing that can open a socket.
"""

from __future__ import annotations

import json

# AccessResolver role constants. The contract accepts only these two — 0 and 3+ revert
# with InvalidRole, and a revert denies the entire condition array (see above).
ROLE_VIEWER = 1
ROLE_CONTRIBUTOR = 2

# Who can decrypt files this skill uploads.
#
# `hasRole` means "at least this role", so CONTRIBUTOR admits Contributors and the Lab
# owner but NOT Viewers. The Labs web app writes ROLE_VIEWER, so a read-only collaborator
# can open files the owner uploads through the app but NOT files uploaded through here.
# That divergence is invisible until a Viewer tries to read, which is why the chosen role
# is printed at upload time and named in the hand-back message.
#
# Flip this one constant (or pass --condition-role viewer) to match the app instead.
DEFAULT_CONDITION_ROLE = ROLE_CONTRIBUTOR

_ROLE_BY_NAME = {"viewer": ROLE_VIEWER, "contributor": ROLE_CONTRIBUTOR}

# Chain strings the backend's evaluator accepts. Anything else throws inside the
# evaluator and, because it is fail-closed, denies access to the whole array. Note that
# the two plausible-looking wrong answers are both silent: "base-sepolia" is not in the
# backend's map, and the chain's own display name is "base sepolia", with a space.
CONDITION_CHAIN_BY_ID = {
    1: "ethereum",
    8453: "base",
    84532: "sepolia-base",
    11155111: "sepolia",
}


def lab_account_address_from_ocl_id(ocl_id: str) -> str:
    """The Lab's token-bound account is the low 20 bytes of the oclId.

    The API computes its `labAccountAddress` field with this same slice, so a local
    derivation cannot diverge from what the API returns — but prefer the API's value when
    you have it and treat a mismatch as a bug worth stopping for.

    oclId layout: [0] version 0x01 | [1] namespace 0x01 | [2..11] tokenId | [12..31] TBA.
    """
    if not (ocl_id.startswith("0x") and len(ocl_id) == 66):
        raise ValueError(f"oclId must be 0x + 64 hex characters, got {ocl_id!r}")
    int(ocl_id, 16)  # reject non-hex
    return "0x" + ocl_id[-40:]


def _has_role_condition(chain: str, resolver: str, ocl_id: str, role: int) -> dict:
    return {
        "chain": chain,
        "conditionType": "evmContract",
        "contractAddress": resolver,
        "functionName": "hasRole",
        # functionParams are STRINGS, including the role. Sending the int 2 here is a
        # silent divergence from every other client.
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


def _tba_owner_condition(chain: str, resolver: str, lab_account_address: str) -> dict:
    return {
        "chain": chain,
        "conditionType": "evmContract",
        "contractAddress": resolver,
        "functionName": "isAuthorizedSignerForTba",
        "functionParams": [":userAddress", lab_account_address],
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


def create_lab_access_conditions(
    *,
    access_resolver_address: str,
    chain_id: int,
    lab_account_address: str,
    ocl_id: str,
    role: int = DEFAULT_CONDITION_ROLE,
) -> list[dict]:
    """The array the Labs app writes for a confidential file:

        hasRole(oclId, caller, role)  OR  isAuthorizedSignerForTba(caller, labAccount)

    The second clause names the Lab's own account explicitly and is the owner's path in.
    Keep it. Whether the first clause alone would admit the owner depends on the chain —
    the contract's own docstring says lab-owner self-administration resolves only on the
    Lab's canonical chain — so relying on it would mean shipping a file whose readability
    by its own owner varies by environment.

    `:userAddress` is a literal placeholder the backend substitutes at decrypt time with
    the service token's adminAddress. Do not fill it in.

    The evaluator walks the array flat and left to right; there is no nesting.
    """
    chain = CONDITION_CHAIN_BY_ID.get(chain_id)
    if not chain:
        raise ValueError(f"No condition chain string for chainId {chain_id}")
    if not access_resolver_address:
        raise ValueError("access_resolver_address is required")
    if not lab_account_address:
        # The Labs app raises here too. Without the owner clause a file can end up
        # readable by this agent and by nobody else — including the human who owns it.
        raise ValueError("lab_account_address is required to build the access conditions")
    if role not in (ROLE_VIEWER, ROLE_CONTRIBUTOR):
        raise ValueError(f"role must be {ROLE_VIEWER} (viewer) or {ROLE_CONTRIBUTOR} (contributor)")
    return [
        _has_role_condition(chain, access_resolver_address, ocl_id, role),
        {"operator": "or"},
        _tba_owner_condition(chain, access_resolver_address, lab_account_address),
    ]


def role_from_name(name: str) -> int:
    try:
        return _ROLE_BY_NAME[name]
    except KeyError:
        raise ValueError(f'condition role must be "viewer" or "contributor", got {name!r}') from None


def to_json(conditions: list[dict]) -> str:
    """Serialize for `encryptionMetadata.accessControlConditions`, which is a String!
    holding a JSON ARRAY — the resolver parses it and rejects anything that is not a list.

    Compact separators match `JSON.stringify`; Python's defaults insert spaces.
    """
    return json.dumps(conditions, separators=(",", ":"))


def assert_conditions_target_lab(
    conditions: list[dict], *, ocl_id: str, lab_account_address: str
) -> bool:
    """Re-read the conditions about to be sent and confirm they name THIS Lab.

    The server never cross-checks this, so it is the only place a copy-paste error is
    caught before it becomes a permanently unreadable file.
    """
    if len(conditions) != 3 or conditions[1] != {"operator": "or"}:
        raise ValueError("access conditions must be [hasRole, {'operator':'or'}, tbaOwner]")
    has_role, _, tba = conditions
    params = has_role.get("functionParams") or []
    if len(params) != 3 or params[0].lower() != ocl_id.lower():
        raise ValueError("access conditions do not name this oclId")
    if params[1] != ":userAddress":
        raise ValueError("access conditions must keep the literal :userAddress placeholder")
    tba_params = tba.get("functionParams") or []
    if len(tba_params) != 2 or tba_params[1].lower() != lab_account_address.lower():
        raise ValueError("access conditions do not name this lab's account address")
    return True
