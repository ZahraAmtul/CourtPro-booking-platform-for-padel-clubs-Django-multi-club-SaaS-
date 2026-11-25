"""Payment gateway adapters.

Each adapter turns an OnlinePayment into a checkout page and verifies the result the gateway
sends back. Adding Easypaisa / Safepay later = one more class with the same two methods.
"""
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone


class GatewayError(Exception):
    """Callback couldn't be trusted (bad signature, unknown reference...)."""


@dataclass
class GatewayResult:
    reference: str
    success: bool
    code: str = ""
    message: str = ""
    amount: Decimal | None = None
    raw: dict = field(default_factory=dict)


class Gateway:
    code = ""
    label = ""
    payment_method = "online"

    def __init__(self, club):
        self.club = club

    def checkout(self, request, attempt):
        raise NotImplementedError

    def parse_callback(self, data: dict) -> GatewayResult:
        raise NotImplementedError

    def return_url(self, request):
        return request.build_absolute_uri(reverse("billing:gateway_return", args=[self.code]))


# --------------------------------------------------------------------------- demo
class DemoGateway(Gateway):
    """Pretend checkout for development and sales demos. Disabled unless PAYMENTS_DEMO_ENABLED."""

    code = "demo"
    label = "Demo checkout"
    salt = "billing.demo-gateway"

    def checkout(self, request, attempt):
        def token(outcome):
            return signing.dumps({"ref": attempt.reference, "ok": outcome}, salt=self.salt)

        return render(request, "billing/demo_checkout.html", {
            "attempt": attempt, "return_url": self.return_url(request),
            "success_token": token(True), "fail_token": token(False),
        })

    def parse_callback(self, data):
        if not settings.PAYMENTS_DEMO_ENABLED:
            raise GatewayError("Demo payments are disabled.")
        try:
            payload = signing.loads(data.get("token", ""), salt=self.salt, max_age=3600)
        except signing.BadSignature as exc:
            raise GatewayError("Invalid demo token") from exc
        return GatewayResult(
            reference=payload["ref"], success=payload["ok"], code="000" if payload["ok"] else "124",
            message="Demo payment approved" if payload["ok"] else "Demo payment declined", raw={"demo": True},
        )


# --------------------------------------------------------------------------- JazzCash
def jazzcash_hash(fields: dict, integrity_salt: str) -> str:
    """pp_SecureHash: HMAC-SHA256 (key = integrity salt) over the salt followed by every non-empty
    pp_* value, sorted by field name and joined with '&'."""
    values = [str(fields[k]) for k in sorted(fields) if k.startswith("pp") and k != "pp_SecureHash"
              and str(fields[k]) != ""]
    message = "&".join([integrity_salt] + values)
    return hmac.new(integrity_salt.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


class JazzCashGateway(Gateway):
    """JazzCash Hosted Checkout (page redirection): customer pays by wallet, card or voucher on
    JazzCash's page, which then POSTs the signed result back to our return URL."""

    code = "jazzcash"
    label = "JazzCash"
    payment_method = "jazzcash"
    SUCCESS_CODES = {"000", "121"}  # 121 = transaction successful (wallet)

    @property
    def url(self):
        return settings.JAZZCASH_SANDBOX_URL if self.club.jazzcash_sandbox else settings.JAZZCASH_LIVE_URL

    def build_fields(self, request, attempt):
        now = timezone.localtime()
        fields = {
            "pp_Version": "1.1",
            "pp_TxnType": "",
            "pp_Language": "EN",
            "pp_MerchantID": self.club.jazzcash_merchant_id,
            "pp_SubMerchantID": "",
            "pp_Password": self.club.jazzcash_password,
            "pp_BankID": "",
            "pp_ProductID": "",
            "pp_TxnRefNo": attempt.reference,
            "pp_Amount": str(int((attempt.amount * 100).to_integral_value())),  # in paisa
            "pp_TxnCurrency": "PKR",
            "pp_TxnDateTime": now.strftime("%Y%m%d%H%M%S"),
            "pp_BillReference": attempt.invoice.number.replace("-", "")[:20],
            "pp_Description": f"{self.club.club_name} court booking"[:100],
            "pp_TxnExpiryDateTime": (now + timedelta(hours=1)).strftime("%Y%m%d%H%M%S"),
            "pp_ReturnURL": self.return_url(request),
            "ppmpf_1": attempt.invoice.bill_to_phone[:20],
        }
        fields["pp_SecureHash"] = jazzcash_hash(fields, self.club.jazzcash_integrity_salt)
        return fields

    def checkout(self, request, attempt):
        return render(request, "billing/redirect_form.html", {
            "action": self.url, "fields": self.build_fields(request, attempt), "gateway": self.label,
        })

    def parse_callback(self, data):
        data = {k: (v[0] if isinstance(v, list) else v) for k, v in dict(data).items()}
        received = data.get("pp_SecureHash", "")
        expected = jazzcash_hash(data, self.club.jazzcash_integrity_salt)
        if not received or not hmac.compare_digest(received.upper(), expected):
            raise GatewayError("JazzCash signature mismatch")
        if data.get("pp_MerchantID") and data["pp_MerchantID"] != self.club.jazzcash_merchant_id:
            raise GatewayError("Unexpected merchant")
        amount = None
        if data.get("pp_Amount", "").isdigit():
            amount = Decimal(data["pp_Amount"]) / 100
        code = data.get("pp_ResponseCode", "")
        return GatewayResult(
            reference=data.get("pp_TxnRefNo", ""), success=code in self.SUCCESS_CODES, code=code,
            message=data.get("pp_ResponseMessage", "")[:255], amount=amount,
            raw={k: v for k, v in data.items() if k not in ("pp_Password", "pp_SecureHash")},
        )


GATEWAYS = {g.code: g for g in (DemoGateway, JazzCashGateway)}


def get_gateway(club, code=None):
    """Adapter for `code` (default: the club's selected gateway), or None if it can't be used."""
    code = club.payment_gateway if code is None else code
    if code == "demo":
        return DemoGateway(club) if settings.PAYMENTS_DEMO_ENABLED else None
    if code == "jazzcash":
        ready = club.jazzcash_merchant_id and club.jazzcash_password and club.jazzcash_integrity_salt
        return JazzCashGateway(club) if ready else None
    return None
