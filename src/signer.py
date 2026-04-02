"""
Signer Module - EIP-712 Order Signing

Fixed against official Polymarket py-clob-client + py-order-utils source:
  https://github.com/Polymarket/py-clob-client
"""

import time
import secrets
from typing import Optional, Dict, Any
from dataclasses import dataclass
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

USDC_DECIMALS = 6

# Polymarket CTF Exchange on Polygon mainnet (chain 137)
# Source: py_clob_client/config.py get_contract_config(137)
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"


@dataclass
class Order:
    token_id: str
    price: float
    size: float
    side: str
    maker: str
    nonce: Optional[int] = None
    fee_rate_bps: int = 0
    signature_type: int = 1  # POLY_PROXY — email/Magic wallet

    def __post_init__(self):
        self.side = self.side.upper()
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {self.side}")
        if not 0 < self.price <= 1:
            raise ValueError(f"Invalid price: {self.price}")
        if self.size <= 0:
            raise ValueError(f"Invalid size: {self.size}")
        if self.nonce is None:
            self.nonce = int(time.time())

        # BUY:  makerAmount = USDC in  (size * price * 1e6)
        #       takerAmount = shares out (size * 1e6)
        # SELL: makerAmount = shares in (size * 1e6)
        #       takerAmount = USDC out  (size * price * 1e6)
        if self.side == "BUY":
            self.maker_amount = int(round(self.size * self.price * 10**USDC_DECIMALS))
            self.taker_amount = int(round(self.size * 10**USDC_DECIMALS))
        else:
            self.maker_amount = int(round(self.size * 10**USDC_DECIMALS))
            self.taker_amount = int(round(self.size * self.price * 10**USDC_DECIMALS))

        self.side_value = 0 if self.side == "BUY" else 1


class SignerError(Exception):
    pass


class OrderSigner:
    # Auth domain (no verifyingContract) — used only for API key derivation
    DOMAIN = {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": 137,
    }

    # Order domain — verified against py_order_utils/builders/base_builder.py
    # _get_domain_separator():
    #   make_domain(name="Polymarket CTF Exchange", version="1",
    #               chainId=str(chain_id), verifyingContract=exchange_address)
    ORDER_DOMAIN = {
        "name": "Polymarket CTF Exchange",   # ← CRITICAL: NOT "ClobAuthDomain"
        "version": "1",
        "chainId": 137,
        "verifyingContract": CTF_EXCHANGE_ADDRESS,
    }

    # Field order must match the Order EIP712Struct in py_order_utils/model/order.py
    ORDER_TYPES = {
        "Order": [
            {"name": "salt",          "type": "uint256"},
            {"name": "maker",         "type": "address"},
            {"name": "signer",        "type": "address"},
            {"name": "taker",         "type": "address"},
            {"name": "tokenId",       "type": "uint256"},
            {"name": "makerAmount",   "type": "uint256"},
            {"name": "takerAmount",   "type": "uint256"},
            {"name": "expiration",    "type": "uint256"},
            {"name": "nonce",         "type": "uint256"},
            {"name": "feeRateBps",    "type": "uint256"},
            {"name": "side",          "type": "uint8"},
            {"name": "signatureType", "type": "uint8"},
        ]
    }

    def __init__(self, private_key: str):
        if private_key.startswith("0x"):
            private_key = private_key[2:]
        try:
            self.wallet = Account.from_key(f"0x{private_key}")
        except Exception as e:
            raise ValueError(f"Invalid private key: {e}")
        self.address = self.wallet.address

    @classmethod
    def from_encrypted(cls, encrypted_data: dict, password: str) -> "OrderSigner":
        from .crypto import KeyManager
        manager = KeyManager()
        private_key = manager.decrypt(encrypted_data, password)
        return cls(private_key)

    def sign_auth_message(self, timestamp: Optional[str] = None, nonce: int = 0) -> str:
        if timestamp is None:
            timestamp = str(int(time.time()))
        auth_types = {
            "ClobAuth": [
                {"name": "address",   "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce",     "type": "uint256"},
                {"name": "message",   "type": "string"},
            ]
        }
        message_data = {
            "address": self.address,
            "timestamp": timestamp,
            "nonce": nonce,
            "message": "This message attests that I control the given wallet",
        }
        signable = encode_typed_data(
            domain_data=self.DOMAIN,
            message_types=auth_types,
            message_data=message_data,
        )
        signed = self.wallet.sign_message(signable)
        return "0x" + signed.signature.hex()

    def sign_order(self, order: Order) -> Dict[str, Any]:
        """
        Sign a Polymarket order, returning exactly what post_order() needs.

        Verified against official sources:
        - Domain: py_order_utils/builders/base_builder.py _get_domain_separator()
        - Order struct fields: py_order_utils/model/order.py Order + SignedOrder.dict()
        - POST body shape: py_clob_client/utilities.py order_to_json()
        """
        try:
            # Random salt — official uses generate_seed() = round(now * random())
            # We use secrets for better entropy; any non-zero random int works
            salt = secrets.randbelow(2**32) + 1  # match official's small int range

            order_message = {
                "salt":          salt,
                "maker":         to_checksum_address(order.maker),
                "signer":        self.address,
                "taker":         "0x0000000000000000000000000000000000000000",
                "tokenId":       int(order.token_id),
                "makerAmount":   order.maker_amount,
                "takerAmount":   order.taker_amount,
                "expiration":    0,
                "nonce":         order.nonce,
                "feeRateBps":    order.fee_rate_bps,
                "side":          order.side_value,
                "signatureType": order.signature_type,
            }

            # Sign using ORDER_DOMAIN — name="Polymarket CTF Exchange" + verifyingContract
            signable = encode_typed_data(
                domain_data=self.ORDER_DOMAIN,
                message_types=self.ORDER_TYPES,
                message_data=order_message,
            )
            signed = self.wallet.sign_message(signable)
            signature = "0x" + signed.signature.hex()

            # Return shape mirrors official SignedOrder.dict() + order_to_json():
            # - All numeric fields as strings (makerAmount, takerAmount, etc.)
            # - side as "BUY" or "SELL" string (NOT "0"/"1") — from SignedOrder.dict()
            # - signature inside the order dict
            # - owner at top level (= maker = funder address)
            return {
                "order": {
                    "salt":          str(salt),
                    "maker":         to_checksum_address(order.maker),
                    "signer":        self.address,
                    "taker":         "0x0000000000000000000000000000000000000000",
                    "tokenId":       order.token_id,           # string token ID
                    "makerAmount":   str(order.maker_amount),
                    "takerAmount":   str(order.taker_amount),
                    "expiration":    "0",
                    "nonce":         str(order.nonce),
                    "feeRateBps":    str(order.fee_rate_bps),
                    "side":          order.side,               # "BUY" or "SELL" string
                    "signatureType": str(order.signature_type),
                    "signature":     signature,
                },
                "owner": to_checksum_address(order.maker),
            }

        except Exception as e:
            raise SignerError(f"Failed to sign order: {e}")

    def sign_order_dict(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        maker: str,
        nonce: Optional[int] = None,
        fee_rate_bps: int = 0,
    ) -> Dict[str, Any]:
        order = Order(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            maker=maker,
            nonce=nonce,
            fee_rate_bps=fee_rate_bps,
        )
        return self.sign_order(order)

    def sign_message(self, message: str) -> str:
        from eth_account.messages import encode_defunct
        signable = encode_defunct(text=message)
        signed = self.wallet.sign_message(signable)
        return "0x" + signed.signature.hex()


WalletSigner = OrderSigner
