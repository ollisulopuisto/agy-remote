"""Self-hosted Web Push Notifications via VAPID."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from .config import get_config

logger = logging.getLogger("agy_remote.push")


class PushManager:
    """Manages VAPID keys and push notification subscriptions."""

    def __init__(self, key_file: Path | None = None) -> None:
        if key_file is None:
            config = get_config()
            key_file = config.brain_dir.parent / "vapid.json"
        self.key_file = key_file
        self.subscriptions: list[dict[str, Any]] = []
        self._load_or_generate_keys()

    def _load_or_generate_keys(self) -> None:
        """Load existing VAPID keys or generate a new keypair."""
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        if self.key_file.exists():
            try:
                with open(self.key_file, encoding="utf-8") as f:
                    data = json.load(f)
                    self.public_key: str = data["public_key"]
                    self.private_key: str = data["private_key"]
                    self.subscriptions = data.get("subscriptions", [])
                    return
            except Exception as e:
                logger.debug("Failed loading VAPID key: %s, generating new one", e)

        vapid = Vapid()
        vapid.generate_keys()
        self.private_key = (
            vapid.private_key.decode("utf-8") if isinstance(vapid.private_key, bytes) else str(vapid.private_key)
        )
        self.public_key = (
            vapid.public_key.decode("utf-8") if isinstance(vapid.public_key, bytes) else str(vapid.public_key)
        )

        self._save()

    def _save(self) -> None:
        """Persist VAPID keys and subscriptions to disk."""
        try:
            with open(self.key_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "public_key": self.public_key,
                        "private_key": self.private_key,
                        "subscriptions": self.subscriptions,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            logger.debug("Failed saving VAPID file: %s", e)

    def add_subscription(self, sub: dict[str, Any]) -> None:
        """Register a new browser push subscription."""
        # Avoid duplicate endpoints
        endpoint = sub.get("endpoint")
        if not endpoint:
            return
        self.subscriptions = [s for s in self.subscriptions if s.get("endpoint") != endpoint]
        self.subscriptions.append(sub)
        self._save()

    def send_notification(self, title: str, body: str, data: dict[str, Any] | None = None) -> None:
        """Send push notification to all registered mobile subscribers."""
        if not self.subscriptions:
            return

        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "data": data or {},
            }
        )

        invalid_endpoints = set()
        for sub in self.subscriptions:
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=self.private_key,
                    vapid_claims={"sub": "mailto:agy-remote@local.host"},
                    timeout=5,
                )
            except WebPushException as ex:
                logger.debug("WebPush error for endpoint: %s", ex)
                if ex.response is not None and ex.response.status_code in (404, 410):
                    invalid_endpoints.add(sub.get("endpoint"))
            except Exception as e:
                logger.debug("Failed sending webpush: %s", e)

        if invalid_endpoints:
            self.subscriptions = [s for s in self.subscriptions if s.get("endpoint") not in invalid_endpoints]
            self._save()


push_manager_instance: PushManager | None = None


def get_push_manager() -> PushManager:
    """Get global push manager singleton."""
    global push_manager_instance
    if push_manager_instance is None:
        push_manager_instance = PushManager()
    return push_manager_instance
