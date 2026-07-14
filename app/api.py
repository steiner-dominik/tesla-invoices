import base64
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Any

import requests
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.auth import REDIRECT_URI
from app.config import Config

logger = logging.getLogger(__name__)

TOKEN_EXPIRATION_THRESHOLD = 7200  # 2 hours
MAX_RETRIES = 3
# (connect, read) timeouts; PDF downloads can be slow on flaky links
REQUEST_TIMEOUT = (10, 90)
OPTION_CODE_SUBSCRIPTION = "$CPF1"
AUTH_HOST = "https://auth.tesla.com"
AUTH_URL = f"{AUTH_HOST}/oauth2/v3/token"

CHARGING_HISTORY_GRAPHQL_URL = "https://akamai-apigateway-charging-ownership.tesla.com/graphql"
# Safety cap for the pagination loop, in case Tesla ever returns hasMoreData=true forever.
CHARGING_HISTORY_MAX_PAGES = 100

# GraphQL query as captured from the mobile app (2026-07).
CHARGING_HISTORY_QUERY = """
    query getChargingHistoryV2(
        $pageNumber: Int!, $sortBy: String, $sortOrder: SortByEnum, $latestSession: Boolean, $vin: String
    ) {
  me {
    charging {
      historyV2(
        pageNumber: $pageNumber
        sortBy: $sortBy
        sortOrder: $sortOrder
        latestSession: $latestSession
        vin: $vin
      ) {
        data {
          ...SparkHistoryItemFragment
        }
        totalResults
        hasMoreData
        pageNumber
      }
    }
  }
}

    fragment SparkHistoryItemFragment on SparkHistoryItem {
  countryCode
  programType
  billingType
  vin
  isMsp
  credit {
    distance
    distanceUnit
  }
  chargingPackage {
    distance
    distanceUnit
    energyApplied
  }
  chargingVoucher {
    voucherValue
  }
  invoices {
    fileName
    contentId
    invoiceType
    invoiceSubType
    label
  }
  chargeSessionId
  siteLocationName
  chargeStartDateTime
  chargeStopDateTime
  unlatchDateTime
  fees {
    ...SparkHistoryFeeFragment
  }
  vehicleMakeType
  sessionId
  surveyCompleted
  surveyType
  postId
  cabinetId
  din
  isDcEnforced
  siteAmenities
  siteEntryLocation {
    latitude
    longitude
  }
  siteAddress {
    ...SparkAddressFragment
  }
  sessionSource
  additionalNotes {
    left
    right
  }
  operator
}

    fragment SparkHistoryFeeFragment on SparkHistoryFee {
  sessionFeeId
  feeType
  payorUid
  amountDue
  currencyCode
  pricingType
  usageBase
  usageTier1
  usageTier2
  usageTier3
  usageTier4
  rateBase
  rateTier1
  rateTier2
  rateTier3
  rateTier4
  totalTier1
  totalTier2
  totalTier3
  totalTier4
  uom
  isPaid
  uid
  totalBase
  totalDue
  netDue
  status
  showPeriods
  outstandingAmount
  paidAmount
  periods {
    ...SparkHistoryFeePeriodsFragment
  }
}

    fragment SparkHistoryFeePeriodsFragment on SparkHistoryFeePeriods {
  sessionFeePeriodId
  startDateTime
  stopDateTime
  actualQuantity
  rate
}


    fragment SparkAddressFragment on SparkAddressType {
  street
  streetNumber
  city
  district
  state
  countryCode
  country
  postalCode
}
    """


class TeslaAuthError(Exception):
    pass


class TeslaAPIError(Exception):
    pass


class TLS13Adapter(HTTPAdapter):
    """Transport adapter that enforces TLS 1.3.

    Tesla hosts reject handshakes below TLS 1.3 with 403 Forbidden — first
    auth.tesla.com (see teslamate-org/teslamate#5406), then also the Owner
    API hosts (see bassmaster187/TeslaLogger@b244443). HTTP/1.1 is still
    accepted for the data endpoints, so this is sufficient for `requests`;
    the token refresh additionally needs a browser TLS fingerprint (see
    TokenManager._auth_post).
    """

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


class TokenManager:
    def __init__(self, config: Config):
        self.config = config
        self.access_token: str = ""
        self.refresh_token: str = ""
        # Access-token-only mode warns once, not on every request of a sync.
        self._expiry_warned = False

    @staticmethod
    def jwt_decode(token: str) -> dict[str, Any]:
        if not token:
            return {}
        try:
            payload = token.split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            # JWT payloads use base64url, not standard base64
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return {}

    @staticmethod
    def _persist_token(path: Path, token: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        # Tokens grant full account access; keep them out of reach of other
        # users on the host (relevant mainly for standalone deployments).
        path.chmod(0o600)

    def _determine_best_token(self, token_type: str, required: bool = True) -> str:
        if token_type == "access":
            path = self.config.access_token_path
            env_token = self.config.env_access_token
        else:
            path = self.config.refresh_token_path
            env_token = self.config.env_refresh_token

        file_token = path.read_text().strip() if path.exists() else ""

        file_token_json = self.jwt_decode(file_token)
        env_token_json = self.jwt_decode(env_token)

        if not file_token_json and not env_token_json:
            if required:
                raise TeslaAuthError(f"Could not find any valid {token_type} token from file or options.")
            return ""

        best_token = env_token
        if file_token_json and env_token_json:
            if file_token_json.get("iat", 0) > env_token_json.get("iat", 0):
                best_token = file_token
        elif file_token_json and not env_token_json:
            best_token = file_token
        elif not file_token_json and env_token_json:
            self._persist_token(path, env_token)

        return best_token

    def has_token(self) -> bool:
        """Read-only check for whether any usable token is configured, for the
        UI's setup state. Unlike load_tokens() it never raises or persists."""
        pairs = (
            (self.config.refresh_token_path, self.config.env_refresh_token),
            (self.config.access_token_path, self.config.env_access_token),
        )
        for path, env_token in pairs:
            if env_token.strip():
                return True
            try:
                if path.exists() and path.read_text().strip():
                    return True
            except OSError:
                pass
        return False

    def load_tokens(self) -> None:
        # A refresh token is recommended (access tokens are then obtained and
        # rotated automatically), but an access token alone also works for
        # standalone users who prefer a short-lived credential — until it
        # expires. At least one of the two must be present.
        previous_access_token = self.access_token
        self.access_token = self._determine_best_token("access", required=False)
        self.refresh_token = self._determine_best_token("refresh", required=False)
        if self.access_token != previous_access_token:
            # A new token deserves its own expiry warning.
            self._expiry_warned = False
        if not self.access_token and not self.refresh_token:
            raise TeslaAuthError(
                "No Tesla token configured. Provide a refresh token (recommended), "
                "or an access token if you prefer a short-lived credential."
            )

    def refresh_access_token_if_needed(self) -> None:
        # In HA mode the user can paste new tokens into the app options at
        # any time, so re-evaluate file vs. options before every cycle.
        if self.config.homeassistant or not self.access_token:
            self.load_tokens()

        # A missing/undecodable access token simply counts as expired (exp 0)
        # and is bootstrapped from the refresh token below.
        jwt_json = self.jwt_decode(self.access_token)
        remaining = jwt_json.get("exp", 0) - time.time()

        if remaining >= TOKEN_EXPIRATION_THRESHOLD:
            return

        if self.refresh_token:
            self.refresh_access_token()
        elif remaining <= 0:
            raise TeslaAuthError(
                "The Tesla access token has expired and no refresh token is configured. "
                "Provide a fresh access token, or configure a refresh token so tokens can be renewed automatically."
            )
        elif not self._expiry_warned:
            # Access-token-only mode: keep using the token until it really expires.
            self._expiry_warned = True
            logger.warning(
                f"The Tesla access token expires in about {int(remaining / 60)} minutes and no refresh token "
                "is configured — syncing will stop working then until a new token is provided"
            )

    @staticmethod
    def _auth_post(payload: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint with a browser TLS fingerprint.

        Tesla fingerprints the TLS ClientHello of the refresh request itself:
        with Python's ssl stack it still answers 200, but the issued access
        token is silently scoped down and then rejected by the Owner API with
        403 "forbidden, see fleet-api" (observed 2026-06, and live here
        2026-07; same as teslamate-org/teslamate#5399, fixed there by
        switching the auth transport, #5406). Neither requests nor httpx
        (even over HTTP/2 + TLS 1.3) pass the check, so the refresh goes
        through curl_cffi impersonating Chrome. The data endpoints are not
        affected and stay on `requests`.
        """
        result = curl_requests.post(AUTH_URL, json=payload, impersonate="chrome", timeout=30)
        result.raise_for_status()
        return result.json()

    def exchange_authorization_code(self, code: str, verifier: str) -> None:
        """Complete an interactive login: swap the authorization code (from the
        UI OAuth flow) for tokens and persist them. The refresh token is what
        every later sync runs on; the access token is a free head start."""
        logger.info("Completing Tesla login (exchanging authorization code)")
        payload = {
            "grant_type": "authorization_code",
            "client_id": "ownerapi",
            "code": code,
            "code_verifier": verifier,
            # Must match the redirect_uri used to build the authorize URL.
            "redirect_uri": REDIRECT_URI,
        }
        try:
            data = self._auth_post(payload)
        except curl_requests.exceptions.RequestException as e:
            raise TeslaAuthError(f"Tesla login failed: {e}") from e

        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise TeslaAuthError(
                "Tesla did not return a refresh token — the login may be incomplete "
                "or the pasted address may be missing the code"
            )
        self.refresh_token = refresh_token
        self._persist_token(self.config.refresh_token_path, refresh_token)
        access_token = data.get("access_token")
        if access_token:
            self.access_token = access_token
            self._persist_token(self.config.access_token_path, access_token)
        self._expiry_warned = False
        logger.info("Tesla login complete; refresh token stored")

    def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise TeslaAuthError(
                "Cannot renew the Tesla access token because no refresh token is configured"
            )
        logger.info("Requesting a new Tesla access token")
        payload = {
            "grant_type": "refresh_token",
            "client_id": "ownerapi",
            "refresh_token": self.refresh_token,
            "scope": "openid email offline_access",
        }
        try:
            data = self._auth_post(payload)
        except curl_requests.exceptions.RequestException as e:
            raise TeslaAuthError(f"Renewing the Tesla access token failed: {e}") from e

        self.access_token = data["access_token"]
        self._persist_token(self.config.access_token_path, self.access_token)
        # Tesla may rotate the refresh token; losing the new one would
        # permanently break auth once the old one is invalidated.
        new_refresh_token = data.get("refresh_token")
        if new_refresh_token and new_refresh_token != self.refresh_token:
            self.refresh_token = new_refresh_token
            self._persist_token(self.config.refresh_token_path, new_refresh_token)
            logger.info("Tesla issued a new refresh token; stored it for future syncs")
        logger.info("Tesla access token renewed")


class TeslaAPIClient:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.sess = requests.Session()
        # allowed_methods=None retries POST too: every request this client
        # makes is a read-only fetch, so replaying is safe. 429 honors
        # Retry-After should Tesla ever rate-limit properly.
        retries = Retry(
            total=MAX_RETRIES, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=None
        )
        # This session only ever talks to Tesla hosts, all of which have
        # started requiring TLS 1.3, so the pin applies to everything.
        self.sess.mount("https://", TLS13Adapter(max_retries=retries))

    def ensure_authenticated(self):
        self.token_manager.refresh_access_token_if_needed()

    def _locale_params(self, vin: str) -> dict[str, str]:
        return {
            "deviceLanguage": "en",
            # Fixed to US: the Tesla app sends US regardless of the account's
            # actual country, and the Akamai GraphQL gateway rejects others.
            "deviceCountry": "US",
            "ttpLocale": "en_US",
            "vin": vin,
        }

    def base_req(self, url: str, method="get", json_data=None, params=None, extra_headers=None) -> Any:
        self.ensure_authenticated()
        logger.debug(f"{method.upper()} Request to url: {url}")

        auth_retried = False
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            # Rebuilt every attempt: a forced refresh below replaces the token.
            headers = {
                "Authorization": f"Bearer {self.token_manager.access_token}",
                **(extra_headers or {}),
            }
            try:
                result = self.sess.request(
                    method=method, url=url, headers=headers, json=json_data, params=params, timeout=REQUEST_TIMEOUT
                )
                result.raise_for_status()
                break
            except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                # The Akamai gateway sometimes kills connections with a bare
                # TCP reset (104) — both idle pooled sockets and, during bot
                # -mitigation episodes, fresh ones. Drop the connection pool
                # so the retry starts from a clean handshake.
                logger.warning(
                    f"Connection to Tesla was interrupted, retrying (attempt {attempt + 1} of {MAX_RETRIES}): {e}"
                )
                last_error = e
                self.sess.close()
                time.sleep(attempt * 3 + 1)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                last_error = e
                # Tesla rejects some tokens before their exp (revoked, or
                # poisoned by a refresh it didn't like); a forced refresh
                # recovers instead of trusting the token until it expires.
                if status in (401, 403) and not auth_retried:
                    auth_retried = True
                    logger.warning(
                        f"Tesla rejected the request (HTTP {status} from {url}); "
                        "renewing the access token and retrying"
                    )
                    self.token_manager.refresh_access_token()
                    continue
                raise TeslaAPIError(f"Request failed: {e}") from e
            except requests.exceptions.RequestException as e:
                raise TeslaAPIError(f"Request failed: {e}") from e
        else:
            raise TeslaAPIError(f"Giving up after {MAX_RETRIES} tries, last error: {last_error}")

        content_type = result.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return result.json()
        elif "application/pdf" in content_type:
            return result.content
        return result

    def get_vehicles(self) -> dict[str, Any]:
        url_products = "https://owner-api.teslamotors.com/api/1/products?orders=1"
        vehicles = {}
        products = self.base_req(url=url_products)
        for product in products.get("response", []):
            if "vin" in product:
                vehicles[product["vin"]] = product
        return vehicles

    def get_charging_history(self, vin: str) -> list[dict[str, Any]]:
        """Fetch all charging history sessions for a VIN, following pagination.

        Tesla replaced the REST charging history endpoint with a paginated
        GraphQL API (observed 2026-07). Intentionally changed; the old endpoint
        was:
            url = "https://ownership.tesla.com/mobile-app/charging/history"
            params = {**self._locale_params(vin), "operationName": "getChargingHistoryV2"}
            response = self.base_req(url, params=params)
        Note: the charging invoice PDF download (get_charging_invoice) and the
        subscription endpoints still use the old ownership.tesla.com REST API.
        """
        params = {
            **self._locale_params(vin),
            "operationName": "getChargingHistoryV2",
            "screen": "charging_history_screen",
        }

        sessions: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = {
                "query": CHARGING_HISTORY_QUERY,
                "variables": {
                    "sortBy": "start_datetime",
                    "sortOrder": "DESC",
                    "pageNumber": page,
                    # Without this GraphQL variable the gateway returns the
                    # account-wide history (the vin in the URL params is not
                    # enough), which would duplicate every invoice once per
                    # vehicle on multi-vehicle accounts.
                    "vin": vin,
                },
                "operationName": "getChargingHistoryV2",
            }
            response = self.base_req(
                CHARGING_HISTORY_GRAPHQL_URL,
                method="post",
                json_data=payload,
                params=params,
                # The Akamai gateway 403s requests that don't look like the
                # mobile app; mirror the captured app headers. The user-agent
                # is load-bearing: without it `requests` sends
                # "python-requests/x", which the gateway rejects even for
                # tokens that pass every other endpoint (seen live 2026-07-05).
                extra_headers={
                    "accept": "*/*",
                    "accept-language": "en",
                    "cache-control": "no-cache",
                    "charset": "utf-8",
                    "user-agent": "okhttp/4.12.0",
                },
            )

            if not isinstance(response, dict):
                raise TeslaAPIError(f"Unexpected charging history response type: {type(response).__name__}")
            if response.get("errors"):
                raise TeslaAPIError(f"Charging history GraphQL error: {response['errors']}")

            history = (((response.get("data") or {}).get("me") or {}).get("charging") or {}).get("historyV2") or {}
            page_data = history.get("data") or []
            sessions.extend(page_data)
            logger.debug(
                "Charging history page %s: %s sessions (total %s, hasMoreData=%s)",
                page,
                len(page_data),
                history.get("totalResults"),
                history.get("hasMoreData"),
            )

            if not history.get("hasMoreData") or not page_data:
                break
            page += 1
            if page > CHARGING_HISTORY_MAX_PAGES:
                logger.warning("Stopping charging history pagination after %s pages", CHARGING_HISTORY_MAX_PAGES)
                break

        logger.debug("Charging history fetched: %s sessions total", len(sessions))
        return sessions

    @staticmethod
    def _expect_pdf(response: Any, what: str) -> bytes:
        """Tesla sometimes answers 200 with a JSON/HTML error body; writing
        that to disk would corrupt the invoice, so fail the single item."""
        if not isinstance(response, (bytes, bytearray)):
            raise TeslaAPIError(f"Expected PDF content for {what}, got {type(response).__name__}")
        content = bytes(response)
        # The %PDF signature must appear near the start (the spec allows a
        # small amount of leading junk) — a PDF Content-Type on an HTML error
        # page must not slip through.
        if b"%PDF" not in content[:1024]:
            raise TeslaAPIError(f"Response for {what} is not a PDF (missing %PDF signature)")
        return content

    def get_charging_invoice(self, invoice_id: str, vin: str) -> bytes:
        url = f"https://ownership.tesla.com/mobile-app/charging/invoice/{invoice_id}"
        response = self.base_req(url, params=self._locale_params(vin))
        return self._expect_pdf(response, f"charging invoice {invoice_id}")

    def get_subscription_invoices(self, vin: str) -> dict[str, Any]:
        url = "https://ownership.tesla.com/mobile-app/subscriptions/invoices"
        params = {**self._locale_params(vin), "optionCode": OPTION_CODE_SUBSCRIPTION}
        response = self.base_req(url, params=params)
        # Only the record count — the raw response carries personal data
        # (billing address etc.) that must not end up in logs.
        if isinstance(response, dict):
            logger.debug("Received %s subscription invoice record(s)", len(response.get("data") or []))
        return response

    def get_subscription_invoice(self, invoice_id: str, vin: str) -> bytes:
        url = f"https://ownership.tesla.com/mobile-app/documents/invoices/{invoice_id}"
        response = self.base_req(url, params=self._locale_params(vin))
        content = self._expect_pdf(response, f"subscription invoice {invoice_id}")
        logger.debug("Subscription invoice PDF response for %s: %s bytes", invoice_id, len(content))
        return content
