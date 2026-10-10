"""The schema of `UserProfile.preferences`. Unknown keys are rejected rather than stored."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class NotificationSettings(BaseModel):
    """Optional emails. Security alerts (new device, password change) are always sent."""

    model_config = ConfigDict(extra="forbid")

    invitations_email: bool = True
    product_updates_email: bool = False


class UserPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: Literal["system", "light", "dark"] = "system"
    notifications: NotificationSettings = NotificationSettings()
