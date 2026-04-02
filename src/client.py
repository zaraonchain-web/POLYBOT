"""
Client Module - API Clients for Polymarket
"""

import time
import hmac
import hashlib
import base64
import json
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

import requests

from .config import BuilderConfig
from .http import ThreadLocalSessionMixin

logger = logging.getLogger(__name__)


class ApiError(Exception):
    pass


class AuthenticationError(ApiError):
    pass


class OrderError(ApiError):
    pass


@dataclass
class ApiCredentials:
    api_key: str
    secret: str
    passphrase: str

    @classmethod
    def load(cls, filepath: str) -> "ApiCredentials":
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(
            api_key=data.get("apiKey", ""),
            secret=data.get("secret", ""),
            passphrase=data.get("passphrase", ""),
        )

    def is_valid(self) -> bool:
        return bool(self.api_key and self.secret and self.passphrase)


class ApiClient(ThreadLocalSessionMixin):
    def __init__(self, base_url: str, timeout: int = 30, retry_count: int = 3):
        super().__init__()
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.retry_count = retry_count

    def _request(self, method: str, endpoint: str, data=None, headers=None, params=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)

        last_error = None
        for attempt in range(self.retry_count):
            try:
                session = self.session
                if method.upper() == "GET":
                    response = session.get(url, headers=request_headers, params=params, timeout=self.timeout)
                elif method.upper() == "POST":
                    response = session.post(url, headers=request_headers, json=data, params=params, timeout=self.timeout)
                elif method.upper() == "DELETE":
                    response = session.delete(url, headers=request_headers, json=data, params=params, timeout=self.timeout)
                else:
                    raise ApiError(f"Unsupported method: {method}")

                response.raise_for_status()
                return response.json() if response.text else {}

            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < self.retry_count - 1:
                    time.sleep(2 ** attempt)

        raise ApiError(f"Request failed after {self.retry_count} attempts: {last_error}")


class ClobClient(ApiClient):
    def __init__(self, host="https://clob.polymarket.com", chain_id=137, signature_type=2,
                 funder="", api_creds=None, builder_creds=None, signer_address=None, timeout=30):
        super().__init__(base_url=host, timeout=timeout)
        self.host = host
        self.chain_id = chain_id
        self.signature_type = signature_type
        self.funder = funder
        self.api_creds = api_creds
        self.builder_creds = builder_creds
        self.signer_address = signer_address

    def _build_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        headers = {}

        if self.builder_creds and self.builder_creds.is_configured():
            timestamp = str(int(time.time()))
            message = f"{timestamp}{method}{path}{body}"
            signature = hmac.new(
                self.builder_creds.api_secret.encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()
            headers.update({
                "POLY_BUILDER_API_KEY": self.builder_creds.api_key,
                "POLY_BUILDER_TIMESTAMP": timestamp,
                "POLY_BUILDER_PASSPHRASE": self.builder_creds.api_passphrase,
                "POLY_BUILDER_SIGNATURE": signature,
            })

        if self.api_creds and self.api_creds.is_valid():
            timestamp = str(int(time.time()))
            message = f"{timestamp}{method}{path}"
            if body:
                message += body

            try:
                base64_secret = base64.urlsafe_b64decode(self.api_creds.secret)
                h = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256)
                signature = base64.urlsafe_b64encode(h.digest()).decode("utf-8")
            except Exception:
                signature = hmac.new(
                    self.api_creds.secret.encode(),
                    message.encode(),
                    hashlib.sha256
                ).hexdigest()

            address = self.signer_address if self.signer_address else self.funder
            headers.update({
                "POLY_ADDRESS": address,
                "POLY_API_KEY": self.api_creds.api_key,
                "POLY_TIMESTAMP": timestamp,
                "POLY_PASSPHRASE": self.api_creds.passphrase,
                "POLY_SIGNATURE": signature,
            })

        return headers

    def derive_api_key(self, signer, nonce=0):
        timestamp = str(int(time.time()))
        auth_signature = signer.sign_auth_message(timestamp=timestamp, nonce=nonce)
        headers = {
            "POLY_ADDRESS": signer.address,
            "POLY_SIGNATURE": auth_signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": str(nonce),
        }
        response = self._request("GET", "/auth/derive-api-key", headers=headers)
        return ApiCredentials(
            api_key=response.get("apiKey", ""),
            secret=response.get("secret", ""),
            passphrase=response.get("passphrase", ""),
        )

    def create_api_key(self, signer, nonce=0):
        timestamp = str(int(time.time()))
        auth_signature = signer.sign_auth_message(timestamp=timestamp, nonce=nonce)
        headers = {
            "POLY_ADDRESS": signer.address,
            "POLY_SIGNATURE": auth_signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": str(nonce),
        }
        response = self._request("POST", "/auth/api-key", headers=headers)
        return ApiCredentials(
            api_key=response.get("apiKey", ""),
            secret=response.get("secret", ""),
            passphrase=response.get("passphrase", ""),
        )

    def create_or_derive_api_key(self, signer, nonce=0):
        try:
            return self.create_api_key(signer, nonce)
        except Exception:
            return self.derive_api_key(signer, nonce)

    def set_api_creds(self, creds, signer_address=None):
        self.api_creds = creds
        if signer_address:
            self.signer_address = signer_address

    def get_fee_rate(self, token_id: str) -> int:
        try:
            result = self._request("GET", "/fee-rate", params={"token_id": token_id})
            return int(result.get("fee_rate_bps", 0))
        except Exception:
            return 0

    def get_order_book(self, token_id: str):
        return self._request("GET", "/book", params={"token_id": token_id})

    def get_market_price(self, token_id: str):
        return self._request("GET", "/price", params={"token_id": token_id})

    def get_open_orders(self):
        endpoint = "/data/orders"
        headers = self._build_headers("GET", endpoint)
        result = self._request("GET", endpoint, headers=headers)
        if isinstance(result, dict) and "data" in result:
            return result.get("data", [])
        return result if isinstance(result, list) else []

    def get_order(self, order_id: str):
        endpoint = f"/data/order/{order_id}"
        headers = self._build_headers("GET", endpoint)
        return self._request("GET", endpoint, headers=headers)

    def get_trades(self, token_id=None, limit=100):
        endpoint = "/data/trades"
        headers = self._build_headers("GET", endpoint)
        params: Dict[str, Any] = {"limit": limit}
        if token_id:
            params["token_id"] = token_id
        result = self._request("GET", endpoint, headers=headers, params=params)
        if isinstance(result, dict) and "data" in result:
            return result.get("data", [])
        return result if isinstance(result, list) else []

    def post_order(self, signed_order: Dict[str, Any], order_type: str = "GTC") -> Dict[str, Any]:
        endpoint = "/order"

        # FIX 4: signer.sign_order() now returns:
        #   { "order": { ...all fields..., "signature": "0x..." }, "owner": "0x..." }
        # We just need to add "orderType" and forward it as-is.
        # The old code was re-wrapping the order and moving the signature to the
        # top level, which broke the API call.
        order_obj = signed_order.get("order", signed_order)
        owner = signed_order.get("owner", self.signer_address if self.signer_address else self.funder)

        body = {
            "order": order_obj,
            "owner": owner,
            "orderType": order_type,
        }

        body_json = json.dumps(body, separators=(',', ':'))
        headers = self._build_headers("POST", endpoint, body_json)

        logger.error(f"DEBUG ORDER BODY: {json.dumps(body, indent=2)}")

        return self._request("POST", endpoint, data=body, headers=headers)

    def cancel_order(self, order_id: str):
        endpoint = "/order"
        body = {"orderID": order_id}
        body_json = json.dumps(body, separators=(',', ':'))
        headers = self._build_headers("DELETE", endpoint, body_json)
        return self._request("DELETE", endpoint, data=body, headers=headers)

    def cancel_orders(self, order_ids: List[str]):
        endpoint = "/orders"
        body_json = json.dumps(order_ids, separators=(',', ':'))
        headers = self._build_headers("DELETE", endpoint, body_json)
        return self._request("DELETE", endpoint, data=order_ids, headers=headers)

    def cancel_all_orders(self):
        endpoint = "/cancel-all"
        headers = self._build_headers("DELETE", endpoint)
        return self._request("DELETE", endpoint, headers=headers)

    def cancel_market_orders(self, market=None, asset_id=None):
        endpoint = "/cancel-market-orders"
        body = {}
        if market:
            body["market"] = market
        if asset_id:
            body["asset_id"] = asset_id
        body_json = json.dumps(body, separators=(',', ':')) if body else ""
        headers = self._build_headers("DELETE", endpoint, body_json)
        return self._request("DELETE", endpoint, data=body if body else None, headers=headers)


class RelayerClient(ApiClient):
    def __init__(self, host="https://relayer-v2.polymarket.com", chain_id=137,
                 builder_creds=None, tx_type="SAFE", timeout=60):
        super().__init__(base_url=host, timeout=timeout)
        self.chain_id = chain_id
        self.builder_creds = builder_creds
        self.tx_type = tx_type

    def _build_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        if not self.builder_creds or not self.builder_creds.is_configured():
            raise AuthenticationError("Builder credentials required for relayer")
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method}{path}{body}"
        signature = hmac.new(
            self.builder_creds.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            "POLY_BUILDER_API_KEY": self.builder_creds.api_key,
            "POLY_BUILDER_TIMESTAMP": timestamp,
            "POLY_BUILDER_PASSPHRASE": self.builder_creds.api_passphrase,
            "POLY_BUILDER_SIGNATURE": signature,
        }

    def deploy_safe(self, safe_address: str):
        endpoint = "/deploy"
        body = {"safeAddress": safe_address}
        body_json = json.dumps(body, separators=(',', ':'))
        headers = self._build_headers("POST", endpoint, body_json)
        return self._request("POST", endpoint, data=body, headers=headers)

    def approve_usdc(self, safe_address: str, spender: str, amount: int):
        endpoint = "/approve-usdc"
        body = {"safeAddress": safe_address, "spender": spender, "amount": str(amount)}
        body_json = json.dumps(body, separators=(',', ':'))
        headers = self._build_headers("POST", endpoint, body_json)
        return self._request("POST", endpoint, data=body, headers=headers)

    def approve_token(self, safe_address: str, token_id: str, spender: str, amount: int):
        endpoint = "/approve-token"
        body = {"safeAddress": safe_address, "tokenId": token_id, "spender": spender, "amount": str(amount)}
        body_json = json.dumps(body, separators=(',', ':'))
        headers = self._build_headers("POST", endpoint, body_json)
        return self._request("POST", endpoint, data=body, headers=headers)
