import asyncio
import logging
from contextlib import asynccontextmanager
from secrets import compare_digest
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from zovak.api.routes.health import router as health_router
from zovak.core.config import get_settings

logger = logging.getLogger(__name__)


def _show_zovak_logs() -> None:
    """Uvicorn only prints its own lines; print ZOVAK's too, with timestamps."""
    zovak_logger = logging.getLogger("zovak")
    if zovak_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    zovak_logger.addHandler(handler)
    zovak_logger.setLevel(logging.INFO)
    zovak_logger.propagate = False


_show_zovak_logs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from zovak.providers.notifications.ntfy_push import NtfyPush
    from zovak.providers.notifications.sms_router import SmsRouter
    from zovak.providers.notifications.telnyx_sms import TelnyxSms
    from zovak.services.booking_forms import BookingForms
    from zovak.services.google_scheduler import GoogleBackedScheduler
    from zovak.workers.reminders import ReminderWorker
    # Load the AI library now (~2.5 s), not during the first patient's first message.
    import agents  # noqa: F401
    from agents.extensions.memory import AsyncSQLiteSession  # noqa: F401
    import zovak.agents.receptionist  # noqa: F401
    app.state.scheduler = GoogleBackedScheduler(app)
    settings = app.state.settings = get_settings()
    if settings.demo_seed:
        from zovak.services.clinic import seed_demo_bookings
        logger.info("Loaded %d demo bookings for the demo doctors", seed_demo_bookings(app.state.scheduler))
    app.state.booking_forms = BookingForms(app.state.scheduler, minutes=settings.booking_form_minutes)
    app.state.sms = sms = SmsRouter()
    if settings.test_sms_ntfy_topic:
        sms.test_push = NtfyPush(settings.test_sms_ntfy_topic, settings.ntfy_server)
    if settings.telnyx_api_key and settings.telnyx_from_number:
        skip = tuple(p.strip() for p in settings.telnyx_sms_skip_prefixes.split(",") if p.strip())
        sms.add_provider("telnyx", TelnyxSms(
            settings.telnyx_api_key, settings.telnyx_from_number,
            settings.telnyx_messaging_profile_id,
        ), skip=skip)
    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        from zovak.providers.notifications.whatsapp_meta import WhatsAppCloud
        from zovak.services.whatsapp_bot import WhatsAppBot
        cloud = WhatsAppCloud(settings.whatsapp_access_token, settings.whatsapp_phone_number_id,
                              graph_version=settings.whatsapp_graph_version)
        app.state.whatsapp = WhatsAppBot(app.state.scheduler, cloud,
                                         max_messages_per_hour=settings.whatsapp_max_messages_per_hour)
        from zovak.services.whatsapp_calls import WhatsAppCalls
        app.state.whatsapp_calls = WhatsAppCalls(app.state, cloud)  # experiment
    from zovak.providers.notifications.resend_email import send_resend_email
    from zovak.services.recovery import RecoveryPlanner
    from zovak.services.staff_auth import StaffSessions

    async def email_sender(to, subject, page, text):
        if settings.resend_api_key and settings.reminder_from_email:
            await send_resend_email(settings.resend_api_key, settings.reminder_from_email, to,
                                    subject, page, text, idempotency_key=f"delay-{to}-{subject}-{text[-40:]}")

    app.state.staff_sessions = StaffSessions()
    app.state.recovery = RecoveryPlanner(
        app.state.scheduler, sms=sms, email_sender=email_sender,
        whatsapp=getattr(getattr(app.state, "whatsapp", None), "cloud", None),
        public_base_url=settings.public_base_url or "http://127.0.0.1:8000",
    )
    # A Bangladeshi gateway plugs in later the same way (Telnyx already skips +880):
    # sms.add_provider("alpha-sms", AlphaSms(...), prefixes=("+880",))
    reminders = None
    if settings.resend_api_key and settings.reminder_from_email:
        worker = ReminderWorker(
            app.state.scheduler, api_key=settings.resend_api_key,
            sender=settings.reminder_from_email, minutes_before=settings.reminder_minutes_before,
        )
        reminders = asyncio.create_task(worker.run_forever(), name="zovak-reminders")
    else:
        logger.info("Appointment reminders off: set RESEND_API_KEY and REMINDER_FROM_EMAIL")
    yield
    if reminders is not None:
        reminders.cancel()
        await asyncio.gather(reminders, return_exceptions=True)


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.enable_dev_routes and settings.app_env != "development":
        raise RuntimeError("Refusing to expose unauthenticated development routes outside development")
    app = FastAPI(title="ZOVAK Backend", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    _guard_public_host(app, settings.public_base_url, settings.site_api_key)

    if settings.enable_dev_routes:
        from zovak.api.routes.google_oauth_dev import router as google_oauth_router
        app.include_router(google_oauth_router)
        from zovak.api.routes.dev import router as dev_router
        app.include_router(dev_router)

        from zovak.api.routes.voice_dev import (
            router as voice_router
        )
        app.include_router(voice_router)

        from zovak.api.routes.forms import router as forms_router
        from zovak.api.routes.phone import router as phone_router
        from zovak.api.routes.whatsapp import router as whatsapp_router
        from zovak.api.routes.staff import router as staff_router
        app.include_router(staff_router)
        app.include_router(forms_router)
        app.include_router(phone_router)
        app.include_router(whatsapp_router)

    return app


# Only these paths may be reached through the public tunnel (PUBLIC_BASE_URL).
PUBLIC_PATHS = ("/api/v1/phone/openai-webhook", "/api/v1/whatsapp/webhook", "/form/", "/update/")


def _guard_public_host(app: FastAPI, public_base_url: str, site_api_key: str = "") -> None:
    """Requests arriving on the public host can only reach the phone webhook and forms.

    The website and its Next.js routes call ZOVAK on 127.0.0.1, so they are unaffected.
    A deployed website (e.g. on Vercel) reaches the other routes through the tunnel only by
    sending the shared site key in X-Zovak-Site-Key; nobody else can.
    """
    public_host = urlparse(public_base_url).hostname if public_base_url else None
    if not public_host:
        return

    @app.middleware("http")
    async def public_only(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        # Tunnels (ngrok) add X-Forwarded-For; local calls from the website don't.
        public = host == public_host.lower() or "x-forwarded-for" in request.headers
        sent_key = request.headers.get("x-zovak-site-key", "")
        from_site = bool(site_api_key) and compare_digest(sent_key.encode(), site_api_key.encode())
        if public and not from_site and not request.url.path.startswith(PUBLIC_PATHS):
            return PlainTextResponse("Not found", status_code=404)
        return await call_next(request)


app = create_app()
