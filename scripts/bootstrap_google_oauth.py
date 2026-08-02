import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from personal_agent.integrations.google_calendar.client import CALENDAR_SCOPES


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize Google Calendar locally and save a refreshable OAuth token."
    )
    parser.add_argument("--client-secret-file", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()

    client_secret_file = Path(args.client_secret_file).resolve()
    token_file = Path(args.token_file).resolve()
    if not client_secret_file.is_file():
        raise SystemExit(f"Client secret file not found: {client_secret_file}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), CALENDAR_SCOPES)
    credentials = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    token_file.chmod(0o600)
    print(f"OAuth token saved to {token_file}")


if __name__ == "__main__":
    main()
