from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.workflows.client import get_mistral_client
from pydantic import BaseModel, Field

from worker.workflows.hn_digest import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_VOICE_ID,
    DIGEST_JSON_SCHEMA,
    SCRIPT_SYSTEM_PROMPT,
    DeliveryResult,
    DigestPlan,
    StoryPick,
    _extract_json,
    _message_text,
    _require_env,
)

ATOM = "{http://www.w3.org/2005/Atom}"


class ArsDigestInput(BaseModel):
    recipient: str | None = Field(
        default=None,
        description="iCloud address that receives the digest. Defaults to the ICLOUD_EMAIL secret.",
    )
    voice_id: str = Field(default=DEFAULT_VOICE_ID, description="Voxtral TTS preset voice slug.")
    max_articles: int = Field(default=8, ge=1, le=15, description="Maximum number of articles in the digest.")
    feed_items: int = Field(default=20, ge=1, le=50, description="How many of the latest RSS items to consider.")
    chat_model: str = Field(default=DEFAULT_CHAT_MODEL, description="Model used for summarizing and scripting.")
    tts_model: str = Field(default=DEFAULT_TTS_MODEL, description="TTS model used for the podcast audio.")
    dry_run: bool = Field(default=False, description="Build the digest and audio but do not send the email.")


class ArsArticle(BaseModel):
    title: str
    link: str
    published: str
    summary: str


class ArsDigestOutput(BaseModel):
    subject: str
    digest_markdown: str
    podcast_script: str
    articles: list[StoryPick]
    delivery: DeliveryResult


ARS_DIGEST_SYSTEM_PROMPT = """You are the editor of a daily digest of Ars Technica articles, built from the subscriber RSS feed.

You receive the latest Ars Technica articles (title, link, published, summary). Select up to {max_articles} of the
most important or interesting tech stories. Skip minor news briefs, rumor roundups, and duplicate coverage of the
same event.

Return JSON matching the provided schema: a short email subject with today's date ('Ars Technica Digest - <date>'),
a markdown digest with one '### headline' section per article (2-3 sentence summary plus the source URL), and the
stories array."""


def _strip_html(text: str) -> str:
    import html
    import re

    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


@workflows.activity(start_to_close_timeout=timedelta(seconds=60), retry_policy_max_attempts=3)
async def fetch_ars_articles(feed_items: int = 20) -> list[ArsArticle]:
    import httpx
    import xml.etree.ElementTree as ET

    rss_url = _require_env("ARS_RSS_URL")
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "ars-digest-workflow/1.0"}) as http:
        response = await http.get(rss_url)
        response.raise_for_status()
        payload = response.content
    root = ET.fromstring(payload)
    entries = root.findall("./channel/item") or root.findall(f"./{ATOM}entry")
    articles: list[ArsArticle] = []
    for entry in entries[:feed_items]:
        title = (entry.findtext("title") or entry.findtext(f"{ATOM}title") or "").strip()
        link = (entry.findtext("link") or "").strip()
        if not link:
            atom_link = entry.find(f"{ATOM}link")
            link = atom_link.get("href", "") if atom_link is not None else ""
        published = (entry.findtext("pubDate") or entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated") or "").strip()
        raw_summary = entry.findtext("description") or entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content") or ""
        summary = _strip_html(raw_summary)[:600]
        if title and link:
            articles.append(ArsArticle(title=title, link=link, published=published, summary=summary))
    if not articles:
        raise RuntimeError("No articles parsed from the Ars RSS feed; check ARS_RSS_URL and the feed format.")
    return articles


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def build_ars_digest(articles: list[ArsArticle], max_articles: int, chat_model: str) -> DigestPlan:
    import json
    from datetime import datetime, timezone

    client = get_mistral_client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    listing = "\n".join(
        f"- [{i}] {a.title} | {a.published} | {a.link} | {a.summary[:300]}" for i, a in enumerate(articles)
    )
    user_prompt = (
        f"Today is {today}. Here are the latest Ars Technica articles (index, title, published, link, summary):\n\n"
        f"{listing}\n\nSelect the top stories and return the digest as JSON."
    )
    system_prompt = ARS_DIGEST_SYSTEM_PROMPT.format(max_articles=max_articles)
    fallback_model = "mistral-medium-latest" if chat_model != "mistral-medium-latest" else "mistral-large-latest"
    attempts = [
        (chat_model, {"type": "json_object"}),
        (chat_model, {"type": "json_schema", "json_schema": {"name": "digest_plan", "schema": DIGEST_JSON_SCHEMA, "strict": True}}),
        (fallback_model, {"type": "json_object"}),
    ]
    plan = None
    last_error = None
    for attempt, (model, response_format) in enumerate(attempts):
        system = system_prompt
        if response_format["type"] == "json_object":
            system += "\nRespond with a single JSON object matching this schema and nothing else:\n" + json.dumps(DIGEST_JSON_SCHEMA)
        response = await client.chat.complete_async(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            response_format=response_format,
            temperature=0.2,
            max_tokens=4096,
        )
        choice = response.choices[0] if response.choices else None
        text = _message_text(choice.message.content) if choice else ""
        try:
            plan = DigestPlan.model_validate_json(_extract_json(text))
            break
        except Exception as exc:
            finish_reason = getattr(choice, "finish_reason", None) if choice else None
            last_error = RuntimeError(
                f"build_ars_digest attempt {attempt + 1} (model={model}) failed: finish_reason={finish_reason!r}, "
                f"content[:200]={text[:200]!r}: {exc}"
            )
    if plan is None:
        raise last_error
    plan.stories = plan.stories[:max_articles]
    if not plan.subject:
        plan.subject = f"Ars Technica Digest - {today}"
    return plan


@workflows.activity(start_to_close_timeout=timedelta(minutes=3), retry_policy_max_attempts=3)
async def write_ars_podcast_script(plan: DigestPlan, chat_model: str) -> str:
    import json
    from datetime import datetime, timezone

    client = get_mistral_client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_prompt = f"Today is {today}. Write the podcast script for this Ars Technica digest:\n\nSubject: {plan.subject}\n\n{plan.digest_markdown}"
    fallback_model = "mistral-medium-latest" if chat_model != "mistral-medium-latest" else "mistral-large-latest"
    script_system = SCRIPT_SYSTEM_PROMPT + '\nRespond with a single JSON object {"script": "<the monologue text>"} and nothing else.'
    last_error = None
    for attempt, model in enumerate([chat_model, fallback_model]):
        response = await client.chat.complete_async(
            model=model,
            messages=[
                {"role": "system", "content": script_system},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=2048,
        )
        choice = response.choices[0] if response.choices else None
        text = _message_text(choice.message.content) if choice else ""
        script = ""
        try:
            payload = json.loads(_extract_json(text))
            if isinstance(payload, dict):
                script = (payload.get("script") or "").strip()
        except Exception as exc:
            last_error = RuntimeError(
                f"write_ars_podcast_script attempt {attempt + 1} (model={model}) invalid JSON: content[:200]={text[:200]!r}: {exc}"
            )
            continue
        if script:
            return script
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        last_error = RuntimeError(
            f"write_ars_podcast_script attempt {attempt + 1} (model={model}) returned empty script (finish_reason={finish_reason!r})"
        )
    raise last_error


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def deliver_ars_digest(
    recipient: str,
    subject: str,
    digest_markdown: str,
    script: str,
    voice_id: str,
    tts_model: str,
    dry_run: bool,
) -> DeliveryResult:
    import base64
    import smtplib
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    client = get_mistral_client()
    speech = await client.audio.speech.complete_async(
        model=tts_model,
        input=script,
        voice_id=voice_id,
        response_format="mp3",
    )
    audio_bytes = base64.b64decode(speech.audio_data)

    if dry_run:
        return DeliveryResult(recipient=recipient, email_sent=False, dry_run=True, audio_bytes=len(audio_bytes))

    sender = _require_env("ICLOUD_EMAIL")
    app_password = _require_env("ICLOUD_APP_PASSWORD")
    recipient = recipient or sender

    message = MIMEMultipart("mixed")
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(digest_markdown, "plain", "utf-8"))

    attachment = MIMEBase("audio", "mpeg")
    attachment.set_payload(audio_bytes)
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename="ars-digest.mp3")
    message.attach(attachment)

    with smtplib.SMTP("smtp.mail.me.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(sender, app_password)
        smtp.send_message(message)

    return DeliveryResult(recipient=recipient, email_sent=True, dry_run=False, audio_bytes=len(audio_bytes))


@workflows.workflow.define(
    name="ars-tech-digest",
    workflow_display_name="Ars Technica Digest",
    workflow_description=(
        "Digest of the latest articles from the Ars Technica subscriber RSS feed (ARS_RSS_URL secret). "
        "Summarizes the top stories, renders a 1-2 minute podcast-style MP3, and emails both to an iCloud inbox."
    ),
)
class ArsTechDigestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: ArsDigestInput) -> ArsDigestOutput:
        articles = await fetch_ars_articles(input.feed_items)
        plan = await build_ars_digest(articles, input.max_articles, input.chat_model)
        script = await write_ars_podcast_script(plan, input.chat_model)
        delivery = await deliver_ars_digest(
            recipient=input.recipient or "",
            subject=plan.subject,
            digest_markdown=plan.digest_markdown,
            script=script,
            voice_id=input.voice_id,
            tts_model=input.tts_model,
            dry_run=input.dry_run,
        )
        return ArsDigestOutput(
            subject=plan.subject,
            digest_markdown=plan.digest_markdown,
            podcast_script=script,
            articles=plan.stories,
            delivery=delivery,
        )
