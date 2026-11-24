import base64
import json
import smtplib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from mistralai.workflows.testing import WorkflowFailureError, create_test_worker

import worker.workflows.ars_digest as ars
import worker.workflows.hn_digest as hn

ARS_RSS_URL = "https://arstechnica.com/feed/?t=SECRET-TOKEN"

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Ars Technica</title>
    <item>
      <title>Apple unveils new Mac chip</title>
      <link>https://arstechnica.com/gadgets/2026/11/apple-mac-chip/</link>
      <pubDate>Fri, 13 Nov 2026 10:00:00 +0000</pubDate>
      <description>&lt;p&gt;Apple announced a &lt;b&gt;new chip&lt;/b&gt; today.&lt;/p&gt;</description>
    </item>
    <item>
      <title>EU fines BigTech over search</title>
      <link>https://arstechnica.com/tech-policy/2026/11/eu-fine/</link>
      <pubDate>Fri, 13 Nov 2026 09:00:00 +0000</pubDate>
      <description>The European Commission fined BigTech 2B euros.</description>
    </item>
    <item>
      <title>News brief: minor update</title>
      <link>https://arstechnica.com/brief/</link>
      <pubDate>Fri, 13 Nov 2026 08:00:00 +0000</pubDate>
      <description>A minor news brief.</description>
    </item>
  </channel>
</rss>
"""

PLAN = {
    "subject": "Ars Technica Digest - 2026-11-13",
    "digest_markdown": "### Apple unveils new Mac chip\n\nApple announced a new chip...\n",
    "stories": [
        {
            "title": "Apple unveils new Mac chip",
            "url": "https://arstechnica.com/gadgets/2026/11/apple-mac-chip/",
            "points": 0,
            "num_comments": 0,
            "summary": "Apple announced a new Mac chip today.",
        }
    ],
}

SCRIPT = "Welcome to the Ars Technica digest. First up: Apple unveiled a new Mac chip..."


class FakeHTTPResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class FakeAsyncClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        FakeAsyncClient.calls.append(url)
        assert url == ARS_RSS_URL, "feed URL must come from the ARS_RSS_URL env var, never hardcoded"
        return FakeHTTPResponse(RSS_XML.encode())


class FakeChat:
    def __init__(self):
        self.calls = []

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        system = kwargs["messages"][0]["content"]
        if "podcast" in system:
            content = json.dumps({"script": SCRIPT})
        else:
            content = json.dumps(PLAN)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class FakeSpeech:
    async def complete_async(self, **kwargs):
        return SimpleNamespace(audio_data=base64.b64encode(b"FAKE-ARS-MP3").decode())


class FakeMistralClient:
    def __init__(self):
        self.chat = FakeChat()
        self.audio = SimpleNamespace(speech=FakeSpeech())


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
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
    monkeypatch.setattr(ars, "get_mistral_client", lambda: fake_client)
    monkeypatch.setattr(hn, "get_mistral_client", lambda: fake_client)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances = []
    FakeAsyncClient.calls = []
    monkeypatch.setenv("ARS_RSS_URL", ARS_RSS_URL)
    monkeypatch.setenv("ICLOUD_EMAIL", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-yyyy-zzzz-wwww")
    return fake_client


async def _run_workflow(temporal_env, workflow_input):
    activities = [ars.fetch_ars_articles, ars.build_ars_digest, ars.write_ars_podcast_script, ars.deliver_ars_digest]
    async with create_test_worker(temporal_env, [ars.ArsTechDigestWorkflow], activities=activities):
        handle = await temporal_env.client.start_workflow(
            "ars-tech-digest",
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
    assert result["delivery"]["audio_bytes"] == len(b"FAKE-ARS-MP3")
    assert result["podcast_script"] == SCRIPT
    assert FakeAsyncClient.calls == [ARS_RSS_URL]

    listing = fake_client.chat.calls[0]["messages"][1]["content"]
    assert "Apple unveils new Mac chip" in listing
    assert "EU fines BigTech over search" in listing
    assert "<p>" not in listing and "&lt;" not in listing
    assert "new chip today" in listing

    assert len(FakeSMTP.instances) == 1
    message = FakeSMTP.instances[0].sent[0]
    assert message["To"] == "me@icloud.com"
    assert message["Subject"] == PLAN["subject"]
    audio_part = next(p for p in message.walk() if p.get_content_type() == "audio/mpeg")
    assert audio_part.get_filename() == "ars-digest.mp3"
    assert base64.b64decode(audio_part.get_payload()) == b"FAKE-ARS-MP3"


async def test_dry_run_skips_email(monkeypatch, temporal_env):
    _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": True})

    assert result["delivery"]["email_sent"] is False
    assert result["delivery"]["dry_run"] is True
    assert FakeSMTP.instances == []


async def test_missing_rss_url_fails_clearly(monkeypatch, temporal_env):
    _install_fakes(monkeypatch)
    monkeypatch.delenv("ARS_RSS_URL")

    with pytest.raises(WorkflowFailureError) as excinfo:
        await _run_workflow(temporal_env, {"dry_run": True})
    msg = str(excinfo.value)
    cause = excinfo.value.cause
    while cause is not None:
        msg += str(cause)
        cause = getattr(cause, "cause", None)
    assert "ARS_RSS_URL" in msg


def test_strip_html():
    assert ars._strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert ars._strip_html("a &amp; b") == "a & b"
