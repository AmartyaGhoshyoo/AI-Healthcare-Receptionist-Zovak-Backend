from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    enable_dev_routes: bool = False
    openai_api_key: str = ""
    openai_agent_model: str = "gpt-6-luna"
    database_url: str = "postgresql+asyncpg://zovak:zovak@localhost:5432/zovak"
    # Appointment reminder emails via Resend. Off until both are set.
    resend_api_key: str = ""
    reminder_from_email: str = ""  # e.g. "Nofa <reminders@zovak.tech>" on a verified domain
    reminder_minutes_before: int = 60
    # Phone calls (GPT-Live SIP). The webhook secret comes from the OpenAI dashboard.
    openai_webhook_secret: str = ""
    # Public https address of this server (e.g. the ngrok URL). Used for SMS form
    # links; requests arriving on this host may only reach the phone/form routes.
    public_base_url: str = ""
    # The deployed website's server sends this in X-Zovak-Site-Key to reach the website routes
    # (chat, voice, staff) through the tunnel. Empty: only the webhooks and forms are public.
    site_api_key: str = ""
    booking_form_minutes: int = 2
    demo_seed: bool = True  # load demo doctors' bookings for today/tomorrow at startup
    max_calls_per_caller_per_hour: int = 5
    # SMS. Testing: a long random ntfy topic pushes every text to the ntfy app.
    test_sms_ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    # Telnyx SMS (works for e.g. Sweden; NOT Bangladesh without a registered sender).
    telnyx_api_key: str = ""
    telnyx_from_number: str = ""
    telnyx_messaging_profile_id: str = ""
    telnyx_sms_skip_prefixes: str = "+880"  # comma-separated country codes Telnyx can't reach
    # WhatsApp Cloud API (Meta). The test number works for free with up to 5 recipients.
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""  # verifies X-Hub-Signature-256 on every webhook
    whatsapp_verify_token: str = ""  # any secret string; also typed into Meta's dashboard
    whatsapp_graph_version: str = "v25.0"
    whatsapp_max_messages_per_hour: int = 30


@lru_cache

def get_settings() -> Settings:
    return Settings()
