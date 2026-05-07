"""AI deep analysis. Tries Claude → Gemini → OpenAI."""
import asyncio
from typing import Optional
from core.models import Signal, MarketSnapshot
from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)


def _build_prompt(sig: Signal, snap: MarketSnapshot) -> str:
    verdicts_summary = "\n".join(
        f"  - {v.name}: score={v.score}, conf={v.confidence:.2f}"
        for v in sig.verdicts
    )
    return f"""You are a senior crypto trading analyst. Analyze SHORTLY in Arabic (max 200 words).

Symbol: {sig.symbol}
Phase: {sig.phase.value}
Confidence: {sig.confidence}/100
Sources Agreed: {sig.sources_agreed}/{sig.sources_total}
Price: ${sig.price}
24h: {sig.change_24h:+.2f}% | Vol: ${sig.volume_24h:,.0f}

Source verdicts:
{verdicts_summary}

Trade plan: Entry ${sig.entry} | SL ${sig.sl} | TP1/2/3 ${sig.tp1}/{sig.tp2}/{sig.tp3}

Provide:
1. ✅ نقاط القوة (2-3 strongest points)
2. ⚠️ المخاطر (2-3 real risks)
3. 🎯 رأيك المباشر: ادخل / انتظر / تجاهل، ولماذا.

Be direct and institutional-grade. No fluff."""


async def analyze_with_claude(sig: Signal, snap: MarketSnapshot) -> Optional[str]:
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": _build_prompt(sig, snap)}],
        )
        return msg.content[0].text if msg.content else None
    except Exception as e:
        log.warning("claude_error", err=str(e))
        return None


async def analyze_with_gemini(sig: Signal, snap: MarketSnapshot) -> Optional[str]:
    if not settings.gemini_api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: model.generate_content(_build_prompt(sig, snap)))
        return resp.text
    except Exception as e:
        log.warning("gemini_error", err=str(e))
        return None


async def analyze_with_openai(sig: Signal, snap: MarketSnapshot) -> Optional[str]:
    if not settings.openai_api_key:
        return None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}",
                         "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini",
                      "messages": [{"role": "user", "content": _build_prompt(sig, snap)}],
                      "max_tokens": 600, "temperature": 0.3},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("openai_error", err=str(e))
    return None


async def deep_analyze(sig: Signal, snap: MarketSnapshot) -> str:
    if not settings.has_ai:
        return "🤖 لم يتم تفعيل أي AI provider."

    for provider, fn in [("Claude", analyze_with_claude),
                          ("Gemini", analyze_with_gemini),
                          ("OpenAI", analyze_with_openai)]:
        result = await fn(sig, snap)
        if result:
            return f"🤖 *تحليل {provider}:*\n\n{result}"
    return "⚠️ فشل الاتصال بـ AI providers."
