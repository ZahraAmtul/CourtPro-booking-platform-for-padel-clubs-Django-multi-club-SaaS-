"""Minimal WhatsApp Cloud API client (Meta). Each club connects its own WhatsApp Business
number, so message charges are billed to the club's own Meta account.

Business-initiated messages must use pre-approved templates (see README for the exact
template text to submit in WhatsApp Manager)."""
import requests
from django.conf import settings

GRAPH_URL = "https://graph.facebook.com/{version}/{phone_id}/messages"


class WhatsAppError(Exception):
    pass


def send_template(club, to: str, template: str, params: list[str], language: str | None = None) -> str:
    """Send a template message and return Meta's message id. Raises WhatsAppError."""
    url = GRAPH_URL.format(version=settings.WHATSAPP_API_VERSION, phone_id=club.whatsapp_phone_number_id)
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": language or club.whatsapp_template_language}},
    }
    if params:
        payload["template"]["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": str(p)[:1000]} for p in params]}
        ]
    try:
        response = requests.post(
            url, json=payload, timeout=10,
            headers={"Authorization": f"Bearer {club.whatsapp_access_token}"},
        )
    except requests.RequestException as exc:
        raise WhatsAppError(f"Network error: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise WhatsAppError(err.get("message") or f"HTTP {response.status_code}")
    try:
        return data["messages"][0]["id"]
    except (KeyError, IndexError):
        raise WhatsAppError("Unexpected response from WhatsApp")
