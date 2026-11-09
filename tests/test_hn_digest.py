import base64
import json
import smtplib
import uuid
from types import SimpleNamespace

import httpx
from mistralai.workflows.testing import create_test_worker

import worker.workflows.hn_digest as hn

ALGOLIA_PAYLOAD = {
    "hits": [
        {
            "objectID": "1001",
            "title": "BigChip unveils next-gen AI accelerator",
            "url": "https://www.theverge.com/2026/1/31/bigchip-accelerator",
            "points": 512,
            "num_comments": 340,
            "author": "reporter1",
            "created_at": "2026-01-31T10:00:00.000Z",
        },
        {
            "objectID": "1002",
            "title": "My personal blog: thoughts on slow living",
            "url": "https://somepersonalblog.wordpress.com/2026/01/30/slow-living",
            "points": 4,
            "num_comments": 2,
            "author": "blogger",
            "created_at": "2026-01-31T09:00:00.000Z",
        },
        {
            "objectID": "1003",
            "title": "Ask HN: What is your morning routine?",
            "url": None,
            "points": 12,
            "num_comments": 58,
            "author": "asker",
            "created_at": "2026-01-31T08:00:00.000Z",
        },
    ]
}

PLAN = {
    "subject": "HN Tech Digest - 2026-01-31",
    "digest_markdown": "### BigChip unveils next-gen AI accelerator\n\nBigChip announced...\n",
    "stories": [
        {
            "title": "BigChip unveils next-gen AI accelerator",
            "url": "https://www.theverge.com/2026/1/31/bigchip-accelerator",
            "points": 512,
            "num_comments": 340,
            "summary": "New accelerator doubles inference throughput.",
        }
    ],
}

SCRIPT = "Welcome to the Hacker News tech digest. First up: BigChip unveiled its next-gen AI accelerator..."


class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        assert url == hn.HN_FRONT_PAGE_URL
        assert params == {"tags": "front_page", "hitsPerPage": 30}
        return FakeHTTPResponse(ALGOLIA_PAYLOAD)


class FakeChat:
    def __init__(self):
        self.calls = []

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("response_format"):
            content = json.dumps(PLAN)
        else:
            content = SCRIPT
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeSpeech:
    def __init__(self):
        self.calls = []

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(audio_data=base64.b64encode(b"FAKE-MP3-BYTES").decode())


class FakeMistralClient:
    def __init__(self):
        self.chat = FakeChat()
        self.audio = SimpleNamespace(speech=FakeSpeech())


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logins = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        return None

    def starttls(self):
        return None

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent.append(message)


def _install_fakes(monkeypatch):
    fake_client = FakeMistralClient()
    monkeypatch.setattr(hn, "get_mistral_client", lambda: fake_client)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances = []
    monkeypatch.setenv("ICLOUD_EMAIL", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-yyyy-zzzz-wwww")
    return fake_client


async def _run_workflow(temporal_env, workflow_input):
    activities = [hn.fetch_front_page, hn.build_digest, hn.write_podcast_script, hn.deliver_digest]
    async with create_test_worker(temporal_env, [hn.HnTechDigestWorkflow], activities=activities):
        handle = await temporal_env.client.start_workflow(
            "hn-tech-digest",
            workflow_input,
            id=f"test-{uuid.uuid4()}",
            task_queue="test-task-queue",
        )
        return await handle.result()


async def test_full_run_sends_email_with_audio(monkeypatch, temporal_env):
    fake_client = _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": False})

    assert result["subject"] == PLAN["subject"]
    assert result["delivery"]["email_sent"] is True
    assert result["delivery"]["recipient"] == "me@icloud.com"
    assert result["delivery"]["audio_bytes"] == len(b"FAKE-MP3-BYTES")
    assert result["podcast_script"] == SCRIPT
    assert result["stories"][0]["title"] == PLAN["stories"][0]["title"]

    digest_call = fake_client.chat.calls[0]
    listing = digest_call["messages"][1]["content"]
    assert "BigChip unveils next-gen AI accelerator" in listing
    assert "author: asker | host: news.ycombinator.com" in listing

    tts_call = fake_client.audio.speech.calls[0]
    assert tts_call["input"] == SCRIPT
    assert tts_call["response_format"] == "mp3"

    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == (hn.ICLOUD_SMTP_HOST, hn.ICLOUD_SMTP_PORT)
    assert smtp.logins == [("me@icloud.com", "xxxx-yyyy-zzzz-wwww")]
    assert len(smtp.sent) == 1
    message = smtp.sent[0]
    assert message["To"] == "me@icloud.com"
    assert message["From"] == "me@icloud.com"
    assert message["Subject"] == PLAN["subject"]
    parts = list(message.walk())
    content_types = [p.get_content_type() for p in parts]
    assert "text/plain" in content_types
    assert "audio/mpeg" in content_types
    audio_part = next(p for p in parts if p.get_content_type() == "audio/mpeg")
    assert audio_part.get_filename() == "hn-tech-digest.mp3"
    assert base64.b64decode(audio_part.get_payload()) == b"FAKE-MP3-BYTES"


async def test_dry_run_skips_email(monkeypatch, temporal_env):
    _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": True})

    assert result["delivery"]["email_sent"] is False
    assert result["delivery"]["dry_run"] is True
    assert result["delivery"]["audio_bytes"] == len(b"FAKE-MP3-BYTES")
    assert FakeSMTP.instances == []


async def test_recipient_override(monkeypatch, temporal_env):
    _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": False, "recipient": "other@me.com"})

    assert result["delivery"]["recipient"] == "other@me.com"
    assert FakeSMTP.instances[0].sent[0]["To"] == "other@me.com"
