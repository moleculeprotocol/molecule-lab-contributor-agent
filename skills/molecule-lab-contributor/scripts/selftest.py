#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cryptography>=42",
# ]
#
# [tool.uv]
# no-build = true   # never compile from source; see agent_upload.py for why
# ///
"""Offline checks for the two modules that decide whether a private file is readable.

    uv run selftest.py

Imports only `kms_envelope` and `access_conditions` — never `labs_api` — so it makes no
network calls and needs no credentials, and its dependency block is just `cryptography`,
so the crypto can be verified without pulling eth-account's C-extension tree.

Plain `unittest`, so it runs on any Python and `pytest` collects it too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import access_conditions as ac  # noqa: E402
import kms_envelope as ke  # noqa: E402

RESOLVER = "0x5493F472602C87318EA5Eff753cDD593bf9bF559"
OCL_ID = "0x010100000000000000002a00aabbccddeeff00112233445566778899aabbccdd"
TBA = "0xaabbccddeeff00112233445566778899aabbccdd"


class KnownAnswer(unittest.TestCase):
    """The frozen vector is the only real cross-implementation check available offline.

    It was produced by a Labs-compatible reference and cross-checked against WebCrypto.
    If these fail, do NOT regenerate the constants to make them pass — the envelope has
    drifted and files written by this build will not open in the Labs app.
    """

    def test_self_test_passes(self):
        self.assertTrue(ke.self_test())

    def test_decrypts_the_frozen_ciphertext(self):
        opened = ke.decrypt(
            base64.b64decode(ke._VECTOR_CIPHERTEXT_B64), ke._VECTOR_IV_B64, ke._VECTOR_KEY_B64
        )
        self.assertEqual(opened, ke._VECTOR_PLAINTEXT)

    def test_reproduces_the_frozen_ciphertext(self):
        again = ke.encrypt(
            ke._VECTOR_PLAINTEXT, ke._VECTOR_KEY_B64, iv=base64.b64decode(ke._VECTOR_IV_B64)
        )
        self.assertEqual(
            base64.b64encode(again.ciphertext).decode(), ke._VECTOR_CIPHERTEXT_B64
        )
        self.assertEqual(again.content_hash, ke._VECTOR_CONTENT_HASH)


class EnvelopeInvariants(unittest.TestCase):
    """One test per row of the crypto-invariants table in SKILL.md."""

    DEK = base64.b64encode(b"0" * 32).decode()

    def test_tag_is_appended_not_separate(self):
        result = ke.encrypt(b"x" * 100, self.DEK)
        self.assertEqual(result.cipher_bytes, 100 + 16)
        self.assertEqual(len(result.ciphertext), 116)

    def test_iv_is_twelve_bytes(self):
        self.assertEqual(len(base64.b64decode(ke.encrypt(b"x", self.DEK).iv)), 12)

    def test_iv_is_fresh_per_call(self):
        self.assertNotEqual(ke.encrypt(b"x", self.DEK).iv, ke.encrypt(b"x", self.DEK).iv)

    def test_dek_must_be_32_bytes(self):
        with self.assertRaises(ValueError):
            ke.encrypt(b"x", base64.b64encode(b"short").decode())

    def test_content_hash_is_of_the_plaintext_not_the_ciphertext(self):
        plaintext = b"round three assay results"
        result = ke.encrypt(plaintext, self.DEK)
        self.assertEqual(result.content_hash, hashlib.sha256(plaintext).hexdigest())
        self.assertNotEqual(result.content_hash, hashlib.sha256(result.ciphertext).hexdigest())

    def test_round_trip(self):
        result = ke.encrypt(b"payload", self.DEK)
        self.assertEqual(ke.decrypt(result.ciphertext, result.iv, self.DEK), b"payload")

    def test_wrong_key_raises(self):
        result = ke.encrypt(b"payload", self.DEK)
        with self.assertRaises(Exception):
            ke.decrypt(result.ciphertext, result.iv, base64.b64encode(b"1" * 32).decode())

    def test_wrong_iv_raises(self):
        result = ke.encrypt(b"payload", self.DEK)
        with self.assertRaises(Exception):
            ke.decrypt(result.ciphertext, base64.b64encode(b"0" * 12).decode(), self.DEK)

    def test_flipped_tag_byte_raises(self):
        result = ke.encrypt(b"payload", self.DEK)
        tampered = bytearray(result.ciphertext)
        tampered[-1] ^= 0x01
        with self.assertRaises(Exception):
            ke.decrypt(bytes(tampered), result.iv, self.DEK)

    def test_truncated_ciphertext_raises(self):
        with self.assertRaises(ValueError):
            ke.decrypt(b"tooshort", base64.b64encode(b"0" * 12).decode(), self.DEK)

    def test_explicit_iv_must_be_twelve_bytes(self):
        with self.assertRaises(ValueError):
            ke.encrypt(b"x", self.DEK, iv=b"0" * 16)


class AccessConditionGoldens(unittest.TestCase):
    """Asserted against a literal JSON string, not a re-derived dict.

    The failure mode here is "uploads fine, never decrypts", which no round-trip catches —
    only a comparison against a value written down independently does.
    """

    EXPECTED = (
        '[{"chain":"sepolia-base","conditionType":"evmContract","contractAddress":'
        '"0x5493F472602C87318EA5Eff753cDD593bf9bF559","functionName":"hasRole",'
        '"functionParams":["0x010100000000000000002a00aabbccddeeff00112233445566778899aabbccdd",'
        '":userAddress","2"],"functionAbi":{"name":"hasRole","inputs":[{"internalType":"bytes32",'
        '"name":"oclId","type":"bytes32"},{"internalType":"address","name":"account",'
        '"type":"address"},{"internalType":"uint8","name":"role","type":"uint8"}],"outputs":'
        '[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":'
        '"function"},"returnValueTest":{"comparator":"=","key":"","value":"true"}},'
        '{"operator":"or"},'
        '{"chain":"sepolia-base","conditionType":"evmContract","contractAddress":'
        '"0x5493F472602C87318EA5Eff753cDD593bf9bF559","functionName":"isAuthorizedSignerForTba",'
        '"functionParams":[":userAddress","0xaabbccddeeff00112233445566778899aabbccdd"],'
        '"functionAbi":{"name":"isAuthorizedSignerForTba","inputs":[{"internalType":"address",'
        '"name":"signer","type":"address"},{"internalType":"address","name":"account",'
        '"type":"address"}],"outputs":[{"internalType":"bool","name":"","type":"bool"}],'
        '"stateMutability":"view","type":"function"},"returnValueTest":{"comparator":"=",'
        '"key":"","value":"true"}}]'
    )

    def _build(self, **overrides):
        params = dict(
            access_resolver_address=RESOLVER, chain_id=84532,
            lab_account_address=TBA, ocl_id=OCL_ID,
        )
        params.update(overrides)
        return ac.create_lab_access_conditions(**params)

    def test_matches_the_golden_json(self):
        self.assertEqual(ac.to_json(self._build()), self.EXPECTED)

    def test_default_role_is_contributor(self):
        self.assertEqual(ac.DEFAULT_CONDITION_ROLE, ac.ROLE_CONTRIBUTOR)

    def test_role_is_a_string_not_an_int(self):
        role = self._build()[0]["functionParams"][2]
        self.assertIsInstance(role, str)
        self.assertEqual(role, "2")

    def test_viewer_role_is_selectable(self):
        conditions = self._build(role=ac.ROLE_VIEWER)
        self.assertEqual(conditions[0]["functionParams"][2], "1")

    def test_shape_is_three_flat_elements_joined_by_or(self):
        conditions = self._build()
        self.assertEqual(len(conditions), 3)
        self.assertEqual(conditions[1], {"operator": "or"})

    def test_user_address_placeholder_is_literal(self):
        conditions = self._build()
        self.assertEqual(conditions[0]["functionParams"][1], ":userAddress")
        self.assertEqual(conditions[2]["functionParams"][0], ":userAddress")

    def test_chain_strings_are_exactly_the_backend_allowlist(self):
        # "base-sepolia" and "base sepolia" are both rejected by the evaluator,
        # which fails closed — so a wrong string here is a permanently unreadable file.
        self.assertEqual(ac.CONDITION_CHAIN_BY_ID[84532], "sepolia-base")
        self.assertEqual(ac.CONDITION_CHAIN_BY_ID[8453], "base")
        self.assertEqual(ac.CONDITION_CHAIN_BY_ID[1], "ethereum")
        self.assertEqual(ac.CONDITION_CHAIN_BY_ID[11155111], "sepolia")

    def test_unknown_chain_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(chain_id=999999)

    def test_invalid_role_is_rejected(self):
        for role in (0, 3, 255):
            with self.assertRaises(ValueError):
                self._build(role=role)

    def test_missing_lab_account_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(lab_account_address="")

    def test_json_is_compact(self):
        self.assertNotIn(", ", ac.to_json(self._build()))

    def test_json_parses_to_a_list(self):
        # The resolver JSON.parses this string and rejects anything that is not an array.
        self.assertIsInstance(json.loads(ac.to_json(self._build())), list)


class OclIdDerivation(unittest.TestCase):
    def test_derives_the_tba(self):
        self.assertEqual(ac.lab_account_address_from_ocl_id(OCL_ID), TBA)

    def test_rejects_a_short_id(self):
        with self.assertRaises(ValueError):
            ac.lab_account_address_from_ocl_id("0xdeadbeef")

    def test_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            ac.lab_account_address_from_ocl_id("0x" + "z" * 64)


class TargetAssertions(unittest.TestCase):
    """Nothing on the server checks that the conditions name the Lab being uploaded to."""

    def _conditions(self, **overrides):
        params = dict(
            access_resolver_address=RESOLVER, chain_id=84532,
            lab_account_address=TBA, ocl_id=OCL_ID,
        )
        params.update(overrides)
        return ac.create_lab_access_conditions(**params)

    def test_accepts_matching_conditions(self):
        self.assertTrue(
            ac.assert_conditions_target_lab(
                self._conditions(), ocl_id=OCL_ID, lab_account_address=TBA
            )
        )

    def test_rejects_a_different_ocl_id(self):
        other = "0x0101" + "11" * 31
        with self.assertRaises(ValueError):
            ac.assert_conditions_target_lab(
                self._conditions(), ocl_id=other, lab_account_address=TBA
            )

    def test_rejects_a_different_lab_account(self):
        with self.assertRaises(ValueError):
            ac.assert_conditions_target_lab(
                self._conditions(), ocl_id=OCL_ID, lab_account_address="0x" + "22" * 20
            )

    def test_rejects_swapped_ocl_id_and_lab_account(self):
        # Swapping them makes the ABI encoding throw inside the evaluator, which fails
        # closed and denies the WHOLE array — including the owner's clause.
        with self.assertRaises(ValueError):
            ac.assert_conditions_target_lab(
                self._conditions(), ocl_id=TBA, lab_account_address=OCL_ID
            )

    def test_rejects_a_substituted_placeholder(self):
        conditions = self._conditions()
        conditions[0]["functionParams"][1] = "0x" + "33" * 20
        with self.assertRaises(ValueError):
            ac.assert_conditions_target_lab(
                conditions, ocl_id=OCL_ID, lab_account_address=TBA
            )

    def test_rejects_a_dropped_owner_clause(self):
        with self.assertRaises(ValueError):
            ac.assert_conditions_target_lab(
                self._conditions()[:1], ocl_id=OCL_ID, lab_account_address=TBA
            )


class NoNetworkImports(unittest.TestCase):
    """The trust property the file split exists to provide."""

    def test_crypto_and_conditions_cannot_reach_the_network(self):
        for module in (ke, ac):
            source = Path(module.__file__).read_text()
            for banned in ("import httpx", "import requests", "import socket", "import urllib"):
                self.assertNotIn(banned, source, f"{module.__name__} must not {banned}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
