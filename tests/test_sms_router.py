"""SMS routing: test inbox always, ntfy push for testing, real provider by country."""
import json

import httpx
import pytest

from zovak.providers.notifications.ntfy_push import NtfyPush
from zovak.providers.notifications.sms_router import SmsRouter
from zovak.providers.notifications.telnyx_sms import TelnyxSms

LINK = 'https://demo.ngrok.app/form/abc123'


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, to, body):
        if self.fail:
            raise RuntimeError('provider down')
        self.sent.append((to, body))
        return 'msg-1'


@pytest.mark.asyncio
async def test_routes_by_country_and_always_keeps_test_inbox():
    router = SmsRouter()
    telnyx, bangla = Recorder(), Recorder()
    router.add_provider('telnyx', telnyx, skip=('+880',))
    await router.send('+46768000000', f'Sweden {LINK}')
    await router.send('+8801712345678', 'Bangladesh')  # Telnyx can't; no BD gateway yet
    assert telnyx.sent == [('+46768000000', f'Sweden {LINK}')]
    assert len(router.messages) == 2
    router.add_provider('bd-gateway', bangla, prefixes=('+880',))
    await router.send('+8801712345678', 'Bangladesh again')
    assert bangla.sent == [('+8801712345678', 'Bangladesh again')]


@pytest.mark.asyncio
async def test_non_numbers_and_failures_only_reach_test_inbox():
    router = SmsRouter()
    router.add_provider('broken', Recorder(fail=True))
    await router.send('alice@sip.linphone.org', 'hi')  # SIP caller: no phone number
    await router.send('+46768000000', 'hi')  # provider fails: logged, not raised
    assert [m.to for m in router.messages] == ['alice@sip.linphone.org', '+46768000000']


@pytest.mark.asyncio
async def test_ntfy_push_makes_the_link_tappable():
    seen = []

    def transport(request):
        seen.append(request)
        return httpx.Response(200, json={'id': 'n1'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        router = SmsRouter()
        router.test_push = NtfyPush('nofa-test-8f3k2', client=client)
        await router.send('+8801712345678', f'Confirm here: {LINK} within 2 minutes')
    request = seen[0]
    assert str(request.url) == 'https://ntfy.sh/nofa-test-8f3k2'
    assert request.headers['Click'] == LINK and '+8801712345678' in request.headers['Title']
    assert LINK in request.content.decode()


@pytest.mark.asyncio
async def test_telnyx_adapter_sends_and_reports_errors():
    bodies = []

    def ok(request):
        bodies.append(json.loads(request.content))
        assert request.headers['Authorization'] == 'Bearer key'
        return httpx.Response(200, json={'data': {'id': 'tx-1', 'to': [{'status': 'queued'}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        sms = TelnyxSms('key', '+18646900191', 'profile-1', client=client)
        assert await sms.send('+46768000000', 'hello') == 'tx-1'
    assert bodies[0] == {'from': '+18646900191', 'to': '+46768000000', 'text': 'hello',
                         'messaging_profile_id': 'profile-1'}

    def rejected(request):
        return httpx.Response(400, json={'errors': [{'code': '40305', 'detail': 'not registered'}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        with pytest.raises(RuntimeError, match='40305: not registered'):
            await TelnyxSms('key', '+1', client=client).send('+8801', 'x')
