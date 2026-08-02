from typing import cast

from fastapi import Request

from personal_agent.core.config import Settings
from personal_agent.services.intake import IntakeService


def get_intake_service(request: Request) -> IntakeService:
    return cast(IntakeService, request.app.state.intake_service)


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)
